from __future__ import annotations

from typing import NamedTuple

import einx
import torch
import torch.nn.functional as F
from einops import einsum, rearrange, repeat
from einops.layers.torch import Rearrange
from torch import nn
from torch.nn import Module

from alphafold3_pytorch.utils.model_utils import (
    concat_previous_window,
    max_neg_value,
    pad_at_dim,
)
from alphafold3_pytorch.utils.typing import Bool, Float, typecheck
from alphafold3_pytorch.utils.utils import default, exists

# config


class AttentionConfig(NamedTuple):
    """Configuration for an attention mechanism."""

    enable_flash: bool
    enable_math: bool
    enable_mem_efficient: bool


# for changing full attention bias matrix to a local windowed one for atom attention


@typecheck
def full_pairwise_repr_to_windowed(
    pairwise_repr: Float["... m m dp"], window_size: int  # type: ignore
) -> Float["... n w (w*2) dp"]:  # type: ignore
    """
    Convert a full pairwise representation matrix to a local windowed one.

    :param pairwise_repr: The full pairwise representation matrix.
    :param window_size: The window size.
    :return: The local windowed pairwise representation matrix.
    """
    seq_len, device = pairwise_repr.shape[-2], pairwise_repr.device

    padding_needed = (window_size - (seq_len % window_size)) % window_size
    pairwise_repr = F.pad(pairwise_repr, (0, 0, 0, padding_needed, 0, padding_needed), value=0.0)
    pairwise_repr = rearrange(
        pairwise_repr, "... (i w1) (j w2) d -> ... i j w1 w2 d", w1=window_size, w2=window_size
    )
    pairwise_repr = concat_previous_window(pairwise_repr, dim_seq=-4, dim_window=-2)

    # get the diagonal

    n = torch.arange(pairwise_repr.shape[-4], device=device)

    pairwise_repr = einx.get_at("... [i j] w1 w2 d, n, n -> ... n w1 w2 d", pairwise_repr, n, n)

    return pairwise_repr


@typecheck
def full_attn_bias_to_windowed(
    attn_bias: Float["... m m"], window_size: int  # type: ignore
) -> Float["... n w (w*2)"]:  # type: ignore
    """
    Convert a full attention bias matrix to a local windowed one.

    :param attn_bias: The full attention bias matrix.
    :param window_size: The window size.
    :return: The local windowed attention bias matrix.
    """
    attn_bias = rearrange(attn_bias, "... -> ... 1")
    attn_bias = full_pairwise_repr_to_windowed(attn_bias, window_size=window_size)
    return rearrange(attn_bias, "... 1 -> ...")


# multi-head attention


class Attention(Module):
    """Attention model."""

    @typecheck
    def __init__(
        self,
        *,
        dim,
        dim_head=64,
        heads=8,
        dropout=0.0,
        gate_output=True,
        query_bias=True,
        flash=True,
        window_size=None,
        num_memory_kv: int = 0,
        efficient_attn_config: AttentionConfig = AttentionConfig(True, True, True),
    ):
        super().__init__()
        """
        ein notation:

        b - batch
        h - heads
        n - sequence
        d - dimension
        e - dimension (pairwise rep)
        i - source sequence
        j - context sequence
        m - memory key / value seq
        """

        dim_inner = dim_head * heads

        self.attend = Attend(
            flash=flash,
            dropout=dropout,
            window_size=window_size,
            attn_config=efficient_attn_config,
        )

        self.split_heads = Rearrange("b n (h d) -> b h n d", h=heads)
        self.merge_heads = Rearrange("b h n d -> b n (h d)")

        self.to_q = nn.Linear(dim, dim_inner, bias=query_bias)
        self.to_kv = nn.Linear(dim, dim_inner * 2, bias=False)
        self.to_out = nn.Linear(dim_inner, dim, bias=False)

        self.memory_kv = None

        if num_memory_kv > 0:
            self.memory_kv = nn.Parameter(torch.zeros(2, heads, num_memory_kv, dim_head))
            nn.init.normal_(self.memory_kv, std=0.02)

        # gating of value
        # allows attention to attend to nothing

        self.to_gates = None

        if gate_output:
            gate_linear = nn.Linear(dim, dim_inner)
            nn.init.zeros_(gate_linear.weight)
            nn.init.constant_(gate_linear.bias, 1.0)

            self.to_gates = gate_linear

    @typecheck
    def forward(
        self,
        seq: Float["b i d"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
        context: Float["b j d"] | None = None,  # type: ignore
        attn_bias: Float["... i j"] | Float["... nw w (w*2)"] | None = None,  # type: ignore
    ) -> Float["b i d"]:  # type: ignore
        """
        Run multi-head attention on a sequence.

        :param seq: The input sequence.
        :param mask: The mask to apply to the sequence.
        :param context: The context sequence to reference.
        :param attn_bias: The attention bias to apply.
        :return: The output sequence.
        """

        q = self.to_q(seq)

        context_seq = default(context, seq)
        k, v = self.to_kv(context_seq).chunk(2, dim=-1)

        q, k, v = tuple(self.split_heads(t) for t in (q, k, v))

        # attention

        out = self.attend(q, k, v, attn_bias=attn_bias, mask=mask, memory_kv=self.memory_kv)

        # merge heads

        out = self.merge_heads(out)

        # gate output

        if exists(self.to_gates):
            gates = self.to_gates(seq)
            out = out * gates.sigmoid()

        # combine heads

        return self.to_out(out)


# attending, both vanilla as well as in-built flash attention


class Attend(Module):
    """Attention module."""

    def __init__(
        self,
        dropout=0.0,
        flash=False,
        window_size=None,
        scale: float | None = None,
        attn_config: AttentionConfig = AttentionConfig(True, True, True),
    ):
        super().__init__()
        """
        ein notation:

        b - batch
        h - heads
        n - sequence
        d - dimension
        e - dimension (pairwise rep)
        i - source sequence
        j - context sequence
        w - local attention windows
        """

        self.scale = scale
        self.dropout = dropout

        self.is_local_attn = exists(window_size)
        self.window_size = window_size

        self.flash = flash
        self.attn_config = attn_config
        self.attn_dropout = nn.Dropout(dropout)

    @typecheck
    def flash_attn(
        self,
        q: Float["b h i d"],  # type: ignore
        k: Float["b h j d"],  # type: ignore
        v: Float["b h j d"],  # type: ignore
        mask: Bool["b j"] | None = None,  # type: ignore
    ) -> Float["b h i d"]:  # type: ignore
        """
        Run flash attention.

        :param q: The query tensor.
        :param k: The key tensor.
        :param v: The value tensor.
        :param mask: The mask to apply to the sequence.
        :return: The output tensor.
        """

        _, heads, seq_len, _ = q.shape

        attn_mask = None

        if exists(mask):
            mask = repeat(mask, "b j -> b h i j", h=heads, i=seq_len)

        with torch.backends.cuda.sdp_kernel(**self.attn_config._asdict()):
            out = F.scaled_dot_product_attention(
                q,
                k,
                v,
                attn_mask=attn_mask,
                scale=self.scale,
                dropout_p=self.dropout if self.training else 0.0,
            )

        return out

    @typecheck
    def local_attn(
        self,
        q: Float["b h n d"],  # type: ignore
        k: Float["b h n d"],  # type: ignore
        v: Float["b h n d"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
        attn_bias: Float["... n n"] | Float["... nw w (w*2)"] | None = None,  # type: ignore
        memory_kv: Float["2 h m d"] | None = None,  # type: ignore
    ) -> Float["b h n d"]:  # type: ignore
        """
        Run simple local attention with a radius of 1 window size.

        :param q: The query tensor.
        :param k: The key tensor.
        :param v: The value tensor.
        :param mask: The mask to apply to the sequence.
        :param attn_bias: The attention bias to apply.
        :param memory_kv: The memory key and value tensors.
        :return: The output tensor.
        """

        window_size, batch, seq_len, device = (
            self.window_size,
            q.shape[0],
            q.shape[-2],
            q.device,
        )

        # constitute mask if not given

        if not exists(mask):
            mask = torch.ones((batch, seq_len), device=device, dtype=torch.bool)

        # pad to multiple of window size if needed

        padding_needed = (window_size - (seq_len % window_size)) % window_size

        if padding_needed > 0:
            q, k, v = tuple(
                pad_at_dim(t, (0, padding_needed), value=0.0, dim=-2) for t in (q, k, v)
            )
            mask = F.pad(mask, (0, padding_needed), value=False)

        # break into windows

        q, k, v = tuple(rearrange(t, "b h (n w) d -> b h n w d", w=window_size) for t in (q, k, v))
        mask = rearrange(mask, "b (n w) -> b n w", w=window_size)

        # just do radius of 1 for now
        # perhaps not even necessary, and could try shifted windows (a la Swin)

        k, v = tuple(pad_at_dim(t, (1, 0), dim=-2) for t in (k, v))
        mask = F.pad(mask, (1, 0), value=False)

        k, v = tuple(torch.cat((t[..., :-1, :], t[..., 1:, :]), dim=-2) for t in (k, v))
        mask = torch.cat((mask[..., :-1], mask[..., 1:]), dim=-1)

        # handle attention bias (inefficiently)

        is_full_attn_bias = attn_bias.shape[-1] == attn_bias.shape[-2]

        if exists(attn_bias) and is_full_attn_bias:
            attn_bias = full_attn_bias_to_windowed(attn_bias, window_size=window_size)

        # carry out attention as usual

        scale = q.shape[-1] ** -0.5

        q = q * scale

        # append memory key / values for local attention windows

        if exists(memory_kv):
            batch, seq, num_mem_kv = k.shape[0], k.shape[2], memory_kv.shape[-2]

            mk, mv = memory_kv
            mk, mv = tuple(repeat(t, "h m d -> b h n m d", b=batch, n=seq) for t in (mk, mv))
            k = torch.cat((mk, k), dim=-2)
            v = torch.cat((mv, v), dim=-2)

            if exists(attn_bias):
                attn_bias = pad_at_dim(attn_bias, (num_mem_kv, 0), value=0.0)

            if exists(mask):
                mask = pad_at_dim(mask, (num_mem_kv, 0), value=True)

        # similarity

        sim = einsum(q, k, "... i d, ... j d -> ... i j")

        if exists(attn_bias):
            if attn_bias.ndim == 4:
                attn_bias = rearrange(attn_bias, "b ... -> b 1 ...")

            assert attn_bias.ndim == sim.ndim
            sim = sim + attn_bias

        sim = einx.where("b n j, b h n i j, -> b h n i j", mask, sim, max_neg_value(sim))

        attn = sim.softmax(dim=-1)

        out = einsum(attn, v, "... i j, ... j d -> ... i d")

        # un-window the output

        out = rearrange(out, "b h n w d -> b h (n w) d")

        # excise the padding for windowing

        out = out[..., :seq_len, :]

        return out

    @typecheck
    def forward(
        self,
        q: Float["b h i d"],  # type: ignore
        k: Float["b h j d"],  # type: ignore
        v: Float["b h j d"],  # type: ignore
        mask: Bool["b j"] | None = None,  # type: ignore
        attn_bias: Float["... i j"] | Float["... nw w (w*2)"] | None = None,  # type: ignore
        memory_kv: Float["2 h m d"] | None = None,  # type: ignore
    ) -> Float["b h i d"]:  # type: ignore
        """
        Run attention.

        :param q: The query tensor.
        :param k: The key tensor.
        :param v: The value tensor.
        :param mask: The mask to apply to the sequence.
        :param attn_bias: The attention bias to apply.
        :param memory_kv: The memory key and value tensors.
        :return: The output tensor.
        """
        is_windowed_attn_bias = None

        if exists(attn_bias):
            is_windowed_attn_bias = attn_bias.shape[-1] != attn_bias.shape[-2]

        # local windowed attention
        # todo (handle attn bias efficiently)

        if self.is_local_attn:
            return self.local_attn(q, k, v, mask=mask, attn_bias=attn_bias, memory_kv=memory_kv)

        assert (
            not exists(is_windowed_attn_bias) or not is_windowed_attn_bias
        ), "Windowed attention bias is not supported with full attention."

        # append memory key / values

        if exists(memory_kv):
            batch, num_mem_kv = q.shape[0], memory_kv.shape[-2]

            mk, mv = memory_kv
            mk, mv = tuple(repeat(t, "h m d -> b h m d", b=batch) for t in (mk, mv))
            k = torch.cat((mk, k), dim=-2)
            v = torch.cat((mv, v), dim=-2)

            if exists(attn_bias):
                attn_bias = pad_at_dim(attn_bias, (num_mem_kv, 0), value=0.0)

            if exists(mask):
                mask = pad_at_dim(mask, (num_mem_kv, 0), value=True)

        # forward to using flash attention if applicable

        can_use_flash = (
            self.flash and not exists(attn_bias),
            "flash attention does not support attention bias with gradients",
        )

        if can_use_flash:
            return self.flash_attn(q, k, v, mask=mask)

        # default attention

        scale = default(self.scale, q.shape[-1] ** -0.5)

        q = q * scale

        # similarity

        sim = einsum(q, k, "b h i d, b h j d -> b h i j")

        # attn bias

        if exists(attn_bias):
            sim = sim + attn_bias

        # masking

        if exists(mask):
            sim = einx.where("b j, b h i j, -> b h i j", mask, sim, max_neg_value(sim))

        # attention

        attn = sim.softmax(dim=-1)
        attn = self.attn_dropout(attn)

        # aggregate values

        out = einsum(attn, v, "b h i j, b h j d -> b h i d")

        return out
