from __future__ import annotations

from functools import partial
from math import pi, sqrt
from typing import Literal, NamedTuple, Tuple

import einx
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import einsum, pack, rearrange, reduce, repeat, unpack
from einops.layers.torch import Rearrange
from taylor_series_linear_attention import TaylorSeriesLinearAttn
from torch import Tensor
from torch.nn import Linear, Module, ModuleList, Sequential
from tqdm import tqdm

from alphafold3_pytorch.models.components.attention import (
    Attention,
    full_attn_bias_to_windowed,
    full_pairwise_repr_to_windowed,
)
from alphafold3_pytorch.utils import RankedLogger
from alphafold3_pytorch.utils.model_utils import (
    concat_neighboring_windows,
    lens_to_mask,
    log,
    max_neg_value,
    maybe,
    mean_pool_with_lens,
    pack_one,
    pad_and_window,
    pad_or_slice_to,
    pad_to_multiple,
    repeat_consecutive_with_lens,
    unpack_one,
)
from alphafold3_pytorch.utils.typing import Bool, Float, Int, typecheck
from alphafold3_pytorch.utils.utils import default, exists

"""
global ein notation:
b - batch
ba - batch with augmentation
h - heads
n - residue sequence length
i - residue sequence length (source)
j - residue sequence length (target)
m - atom sequence length
nw - windowed sequence length
d - feature dimension
ds - feature dimension (single)
dp - feature dimension (pairwise)
dap - feature dimension (atompair)
dapi - feature dimension (atompair input)
da - feature dimension (atom)
dai - feature dimension (atom input)
t - templates
s - msa
r - registers
"""

"""
additional_residue_feats: [*, 10]:
0: residue_index
1: token_index
2: asym_id
3: entity_id
4: sym_id
5: restype (must be one hot encoded to 32)
6: is_protein
7: is_rna
8: is_dna
9: is_ligand
"""

# constants

ADDITIONAL_RESIDUE_FEATS = 10

LinearNoBias = partial(Linear, bias=False)

logger = RankedLogger(__name__, rank_zero_only=True)

# linear and outer sum
# for single repr -> pairwise pattern throughout this architecture


class LinearNoBiasThenOuterSum(Module):
    """LinearNoBias module followed by outer sum."""

    def __init__(self, dim, dim_out=None):
        super().__init__()
        dim_out = default(dim_out, dim)
        self.proj = LinearNoBias(dim, dim_out * 2)

    @typecheck
    def forward(
        self, t: Float["b n ds"]  # type: ignore
    ) -> Float["b n n dp"]:  # type: ignore
        """
        Perform the forward pass.

        :param t: The input tensor.
        :return: The output tensor.
        """
        single_i, single_j = self.proj(t).chunk(2, dim=-1)
        out = einx.add("b i d, b j d -> b i j d", single_i, single_j)
        return out


# classic feedforward, SwiGLU variant
# they name this "transition" in their paper
# Algorithm 11


class SwiGLU(Module):
    """Swish-Gated Linear Unit."""

    @typecheck
    def forward(
        self, x: Float["... d"]  # type: ignore
    ) -> Float[" ... (d//2)"]:  # type: ignore
        """
        Perform the forward pass.

        :param x: The input tensor.
        :return: The output tensor.
        """
        x, gates = x.chunk(2, dim=-1)
        return F.silu(gates) * x


class Transition(Module):
    """A Transition module."""

    def __init__(self, *, dim, expansion_factor=4):
        super().__init__()
        dim_inner = int(dim * expansion_factor)

        self.ff = Sequential(
            LinearNoBias(dim, dim_inner * 2),
            SwiGLU(),
            LinearNoBias(dim_inner, dim),
        )

    @typecheck
    def forward(
        self, x: Float["... d"]  # type: ignore
    ) -> Float["... d"]:  # type: ignore
        """
        Perform the forward pass.

        :param x: The input tensor.
        :return: The output tensor.
        """
        return self.ff(x)


# dropout
# they seem to be using structured dropout - row / col wise in triangle modules


class Dropout(Module):
    """A Dropout module."""

    @typecheck
    def __init__(self, prob: float, *, dropout_type: Literal["row", "col"] | None = None):
        super().__init__()
        self.dropout = nn.Dropout(prob)
        self.dropout_type = dropout_type

    @typecheck
    def forward(self, t: Tensor) -> Tensor:
        """
        Perform the forward pass.

        :param t: The input tensor.
        :return: The output tensor.
        """
        if self.dropout_type in {"row", "col"}:
            assert (
                t.ndim == 4
            ), "Tensor `t` must consist of 4 dimensions for row/col structured dropout."

        if not exists(self.dropout_type):
            return self.dropout(t)

        if self.dropout_type == "row":
            batch, row, _, _ = t.shape
            ones_shape = (batch, row, 1, 1)

        elif self.dropout_type == "col":
            batch, _, col, _ = t.shape
            ones_shape = (batch, 1, col, 1)

        ones = t.new_ones(ones_shape)
        dropped = self.dropout(ones)
        return t * dropped


# normalization
# both pre layernorm as well as adaptive layernorm wrappers


class PreLayerNorm(Module):
    """A Pre-LayerNorm module."""

    @typecheck
    def __init__(
        self,
        fn: Attention
        | Transition
        | TriangleAttention
        | TriangleMultiplication
        | AttentionPairBias,
        *,
        dim,
    ):
        super().__init__()
        self.fn = fn
        self.norm = nn.LayerNorm(dim)

    @typecheck
    def forward(
        self, x: Float["... n d"], **kwargs  # type: ignore
    ) -> Float["... n d"]:  # type: ignore
        """
        Perform the forward pass.

        :param x: The input tensor.
        :return: The output tensor.
        """
        x = self.norm(x)
        return self.fn(x, **kwargs)


class AdaptiveLayerNorm(Module):
    """Algorithm 26."""

    def __init__(self, *, dim, dim_cond):
        super().__init__()
        self.norm = nn.LayerNorm(dim, elementwise_affine=False)
        self.norm_cond = nn.LayerNorm(dim_cond, bias=False)

        self.to_gamma = nn.Sequential(Linear(dim_cond, dim), nn.Sigmoid())

        self.to_beta = LinearNoBias(dim_cond, dim)

    @typecheck
    def forward(
        self,
        x: Float["b n d"],  # type: ignore
        cond: Float["b n dc"],  # type: ignore
    ) -> Float["b n d"]:  # type: ignore
        """
        Perform the forward pass.

        :param x: The input tensor.
        :param cond: The conditional tensor.
        :return: The output tensor.
        """
        normed = self.norm(x)
        normed_cond = self.norm_cond(cond)

        gamma = self.to_gamma(normed_cond)
        beta = self.to_beta(normed_cond)
        return normed * gamma + beta


class ConditionWrapper(Module):
    """Algorithm 25."""

    @typecheck
    def __init__(
        self,
        fn: Attention | Transition | TriangleAttention | AttentionPairBias,
        *,
        dim,
        dim_cond,
        adaln_zero_bias_init_value=-2.0,
    ):
        super().__init__()
        self.fn = fn
        self.adaptive_norm = AdaptiveLayerNorm(dim=dim, dim_cond=dim_cond)

        adaln_zero_gamma_linear = Linear(dim_cond, dim)
        nn.init.zeros_(adaln_zero_gamma_linear.weight)
        nn.init.constant_(adaln_zero_gamma_linear.bias, adaln_zero_bias_init_value)

        self.to_adaln_zero_gamma = nn.Sequential(adaln_zero_gamma_linear, nn.Sigmoid())

    @typecheck
    def forward(
        self,
        x: Float["b n d"],  # type: ignore
        *,
        cond: Float["b n dc"],  # type: ignore
        **kwargs,
    ) -> Float["b n d"]:  # type: ignore
        """
        Perform the forward pass.

        :param x: The input tensor.
        :param cond: The conditional tensor.
        :return: The output tensor.
        """
        x = self.adaptive_norm(x, cond=cond)

        out = self.fn(x, **kwargs)

        gamma = self.to_adaln_zero_gamma(cond)
        return out * gamma


# triangle multiplicative module
# seems to be unchanged from alphafold2


class TriangleMultiplication(Module):
    """A TriangleMultiplication module from AlphaFold 2."""

    @typecheck
    def __init__(
        self,
        *,
        dim,
        dim_hidden=None,
        mix: Literal["incoming", "outgoing"] = "incoming",
        dropout=0.0,
        dropout_type: Literal["row", "col"] | None = None,
    ):
        super().__init__()

        dim_hidden = default(dim_hidden, dim)
        self.norm = nn.LayerNorm(dim)

        self.left_right_proj = nn.Sequential(LinearNoBias(dim, dim_hidden * 4), nn.GLU(dim=-1))

        self.left_right_gate = LinearNoBias(dim, dim_hidden * 2)

        self.out_gate = LinearNoBias(dim, dim_hidden)

        if mix == "outgoing":
            self.mix_einsum_eq = "... i k d, ... j k d -> ... i j d"
        elif mix == "incoming":
            self.mix_einsum_eq = "... k j d, ... k i d -> ... i j d"

        self.to_out_norm = nn.LayerNorm(dim_hidden)

        self.to_out = Sequential(
            LinearNoBias(dim_hidden, dim),
            Dropout(dropout, dropout_type=dropout_type),
        )

    @typecheck
    def forward(
        self,
        x: Float["b n n d"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
    ) -> Float["b n n d"]:  # type: ignore
        """
        Perform the forward pass.

        :param x: The input tensor.
        :param mask: The mask tensor.
        :return: The output tensor.
        """
        if exists(mask):
            mask = einx.logical_and("b i, b j -> b i j 1", mask, mask)

        x = self.norm(x)

        left, right = self.left_right_proj(x).chunk(2, dim=-1)

        if exists(mask):
            left = left * mask
            right = right * mask

        out = einsum(left, right, self.mix_einsum_eq)

        out = self.to_out_norm(out)

        out_gate = self.out_gate(x).sigmoid()
        out = out * out_gate

        return self.to_out(out)


# there are two types of attention in this paper, triangle and attention-pair-bias
# they differ by how the attention bias is computed
# triangle is axial attention w/ itself projected for bias


class AttentionPairBias(Module):
    """An Attention module with pair bias computation."""

    def __init__(self, *, heads, dim_pairwise, window_size=None, **attn_kwargs):
        super().__init__()

        self.window_size = window_size

        self.attn = Attention(heads=heads, window_size=window_size, **attn_kwargs)

        # line 8 of Algorithm 24

        to_attn_bias_linear = LinearNoBias(dim_pairwise, heads)
        nn.init.zeros_(to_attn_bias_linear.weight)

        self.to_attn_bias = nn.Sequential(
            nn.LayerNorm(dim_pairwise), to_attn_bias_linear, Rearrange("b ... h -> b h ...")
        )

    @typecheck
    def forward(
        self,
        single_repr: Float["b n ds"],  # type: ignore
        *,
        pairwise_repr: Float["b n n dp"] | Float["b nw w (w*3) dp"],  # type: ignore
        attn_bias: Float["b n n"] | Float["b nw w (w*3)"] | None = None,  # type: ignore
        **kwargs,
    ) -> Float["b n ds"]:  # type: ignore
        """
        Perform the forward pass.

        :param single_repr: The single representation tensor.
        :param pairwise_repr: The pairwise representation tensor.
        :param attn_bias: The attention bias tensor.
        :return: The output tensor.
        """
        w, has_window_size = self.window_size, exists(self.window_size)

        # take care of windowing logic
        # for sequence-local atom transformer

        windowed_pairwise = pairwise_repr.ndim == 5

        windowed_attn_bias = None

        if exists(attn_bias):
            windowed_attn_bias = attn_bias.shape[-1] != attn_bias.shape[-2]

        if has_window_size:
            if not windowed_pairwise:
                pairwise_repr = full_pairwise_repr_to_windowed(pairwise_repr, window_size=w)
            if exists(attn_bias):
                attn_bias = full_attn_bias_to_windowed(attn_bias, window_size=w)
        else:
            assert (
                not windowed_pairwise
            ), "Cannot pass in windowed pairwise representation if no `window_size` given to `AttentionPairBias`."
            assert (
                not exists(windowed_attn_bias) or not windowed_attn_bias
            ), "Cannot pass in windowed attention bias if no `window_size` is set for `AttentionPairBias`."

        # attention bias preparation with further addition from pairwise repr

        if exists(attn_bias):
            attn_bias = rearrange(attn_bias, "b ... -> b 1 ...")
        else:
            attn_bias = 0.0

        attn_bias = self.to_attn_bias(pairwise_repr) + attn_bias

        out = self.attn(single_repr, attn_bias=attn_bias, **kwargs)

        return out


class TriangleAttention(Module):
    """An Attention module with triangular bias computation."""

    def __init__(
        self,
        *,
        dim,
        heads,
        node_type: Literal["starting", "ending"],
        dropout=0.0,
        dropout_type: Literal["row", "col"] | None = None,
        **attn_kwargs,
    ):
        super().__init__()
        self.need_transpose = node_type == "ending"

        self.attn = Attention(dim=dim, heads=heads, **attn_kwargs)

        self.dropout = Dropout(dropout, dropout_type=dropout_type)

        self.to_attn_bias = nn.Sequential(
            LinearNoBias(dim, heads), Rearrange("... i j h -> ... h i j")
        )

    @typecheck
    def forward(
        self,
        pairwise_repr: Float["b n n d"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
        **kwargs,
    ) -> Float["b n n d"]:  # type: ignore
        """
        Perform the forward pass.

        :param pairwise_repr: The pairwise representation tensor.
        :param mask: The mask tensor.
        :return: The output tensor.
        """
        if self.need_transpose:
            pairwise_repr = rearrange(pairwise_repr, "b i j d -> b j i d")

        attn_bias = self.to_attn_bias(pairwise_repr)

        batch_repeat = pairwise_repr.shape[1]
        attn_bias = repeat(attn_bias, "b ... -> (b repeat) ...", repeat=batch_repeat)

        if exists(mask):
            mask = repeat(mask, "b ... -> (b repeat) ...", repeat=batch_repeat)

        pairwise_repr, packed_shape = pack_one(pairwise_repr, "* n d")

        out = self.attn(pairwise_repr, mask=mask, attn_bias=attn_bias, **kwargs)

        out = unpack_one(out, packed_shape, "* n d")

        if self.need_transpose:
            out = rearrange(out, "b j i d -> b i j d")

        return self.dropout(out)


# PairwiseBlock
# used in both MSAModule and Pairformer
# consists of all the "Triangle" modules + Transition


class PairwiseBlock(Module):
    """A PairwiseBlock module."""

    def __init__(
        self,
        *,
        dim_pairwise=128,
        tri_mult_dim_hidden=None,
        tri_attn_dim_head=32,
        tri_attn_heads=4,
        dropout_row_prob=0.25,
        dropout_col_prob=0.25,
    ):
        super().__init__()

        pre_ln = partial(PreLayerNorm, dim=dim_pairwise)

        tri_mult_kwargs = dict(dim=dim_pairwise, dim_hidden=tri_mult_dim_hidden)

        tri_attn_kwargs = dict(dim=dim_pairwise, heads=tri_attn_heads, dim_head=tri_attn_dim_head)

        self.tri_mult_outgoing = pre_ln(
            TriangleMultiplication(
                mix="outgoing",
                dropout=dropout_row_prob,
                dropout_type="row",
                **tri_mult_kwargs,
            )
        )
        self.tri_mult_incoming = pre_ln(
            TriangleMultiplication(
                mix="incoming",
                dropout=dropout_row_prob,
                dropout_type="row",
                **tri_mult_kwargs,
            )
        )
        self.tri_attn_starting = pre_ln(
            TriangleAttention(
                node_type="starting",
                dropout=dropout_row_prob,
                dropout_type="row",
                **tri_attn_kwargs,
            )
        )
        self.tri_attn_ending = pre_ln(
            TriangleAttention(
                node_type="ending",
                dropout=dropout_col_prob,
                dropout_type="col",
                **tri_attn_kwargs,
            )
        )
        self.pairwise_transition = pre_ln(Transition(dim=dim_pairwise))

    @typecheck
    def forward(
        self,
        *,
        pairwise_repr: Float["b n n d"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
    ):
        """
        Perform the forward pass.

        :param pairwise_repr: The pairwise representation tensor.
        :param mask: The mask tensor.
        :return: The output tensor.
        """
        pairwise_repr = self.tri_mult_outgoing(pairwise_repr, mask=mask) + pairwise_repr
        pairwise_repr = self.tri_mult_incoming(pairwise_repr, mask=mask) + pairwise_repr
        pairwise_repr = self.tri_attn_starting(pairwise_repr, mask=mask) + pairwise_repr
        pairwise_repr = self.tri_attn_ending(pairwise_repr, mask=mask) + pairwise_repr

        pairwise_repr = self.pairwise_transition(pairwise_repr) + pairwise_repr
        return pairwise_repr


# msa module


class OuterProductMean(Module):
    """Algorithm 9."""

    def __init__(self, *, dim_msa=64, dim_pairwise=128, dim_hidden=32, eps=1e-5):
        super().__init__()
        self.eps = eps
        self.norm = nn.LayerNorm(dim_msa)
        self.to_hidden = LinearNoBias(dim_msa, dim_hidden * 2)
        self.to_pairwise_repr = nn.Linear(dim_hidden**2, dim_pairwise)

    @typecheck
    def forward(
        self,
        msa: Float["b s n d"],  # type: ignore
        *,
        mask: Bool["b n"] | None = None,  # type: ignore
        msa_mask: Bool["b s"] | None = None,  # type: ignore
    ) -> Float["b n n dp"]:  # type: ignore
        """
        Perform the forward pass.

        :param msa: The MSA tensor.
        :param mask: The mask tensor.
        :param msa_mask: The MSA mask tensor.
        :return: The output tensor.
        """
        msa = self.norm(msa)

        # line 2

        a, b = self.to_hidden(msa).chunk(2, dim=-1)

        outer_product = einsum(a, b, "b s i d, b s j e -> b i j d e s")

        # maybe masked mean for outer product

        if exists(msa_mask):
            outer_product = einx.multiply(
                "b i j d e s, b s -> b i j d e s",
                outer_product,
                msa_mask.float(),
            )

            num = reduce(outer_product, "... s -> ...", "sum")
            den = reduce(msa_mask.float(), "... s -> ...", "sum")

            outer_product_mean = einx.divide("b i j d e, b", num, den.clamp(min=self.eps))
        else:
            outer_product_mean = reduce(outer_product, "... s -> ...", "mean")

        # flatten

        outer_product_mean = rearrange(outer_product_mean, "... d e -> ... (d e)")

        # masking for pairwise repr

        if exists(mask):
            mask = einx.logical_and("b i , b j -> b i j 1", mask, mask)
            outer_product_mean = outer_product_mean * mask

        pairwise_repr = self.to_pairwise_repr(outer_product_mean)
        return pairwise_repr


class MSAPairWeightedAveraging(Module):
    """Algorithm 10."""

    def __init__(
        self,
        *,
        dim_msa=64,
        dim_pairwise=128,
        dim_head=32,
        heads=8,
        dropout=0.0,
        dropout_type: Literal["row", "col"] | None = None,
    ):
        super().__init__()
        dim_inner = dim_head * heads

        self.msa_to_values_and_gates = nn.Sequential(
            nn.LayerNorm(dim_msa),
            LinearNoBias(dim_msa, dim_inner * 2),
            Rearrange("b s n (gv h d) -> gv b h s n d", gv=2, h=heads),
        )

        self.pairwise_repr_to_attn = nn.Sequential(
            nn.LayerNorm(dim_pairwise),
            LinearNoBias(dim_pairwise, heads),
            Rearrange("b i j h -> b h i j"),
        )

        self.to_out = nn.Sequential(
            Rearrange("b h s n d -> b s n (h d)"),
            LinearNoBias(dim_inner, dim_msa),
            Dropout(dropout, dropout_type=dropout_type),
        )

    @typecheck
    def forward(
        self,
        *,
        msa: Float["b s n d"],  # type: ignore
        pairwise_repr: Float["b n n dp"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
    ) -> Float["b s n d"]:  # type: ignore
        """
        Perform the forward pass.

        :param msa: The MSA tensor.
        :param pairwise_repr: The pairwise representation tensor.
        :param mask: The mask tensor.
        :return: The output tensor.
        """
        values, gates = self.msa_to_values_and_gates(msa)
        gates = gates.sigmoid()

        # line 3

        b = self.pairwise_repr_to_attn(pairwise_repr)

        if exists(mask):
            mask = rearrange(mask, "b j -> b 1 1 j")
            b = b.masked_fill(~mask, max_neg_value(b))

        # line 5

        weights = b.softmax(dim=-1)

        # line 6

        out = einsum(weights, values, "b h i j, b h s j d -> b h s i d")

        out = out * gates

        # combine heads

        return self.to_out(out)


class MSAModule(Module):
    """Algorithm 8."""

    def __init__(
        self,
        *,
        dim_single=384,
        dim_pairwise=128,
        depth=4,
        dim_msa=64,
        dim_msa_input=None,
        outer_product_mean_dim_hidden=32,
        msa_pwa_dropout_row_prob=0.15,
        msa_pwa_heads=8,
        msa_pwa_dim_head=32,
        pairwise_block_kwargs: dict = dict(),
        max_num_msa: int | None = None,
    ):
        super().__init__()

        self.max_num_msa = default(
            max_num_msa, float("inf")
        )  # cap the number of MSAs, will do sample without replacement if exceeds

        self.msa_init_proj = (
            LinearNoBias(dim_msa_input, dim_msa) if exists(dim_msa_input) else nn.Identity()
        )

        self.single_to_msa_feats = LinearNoBias(dim_single, dim_msa)

        layers = ModuleList([])

        for _ in range(depth):
            msa_pre_ln = partial(PreLayerNorm, dim=dim_msa)

            outer_product_mean = OuterProductMean(
                dim_msa=dim_msa,
                dim_pairwise=dim_pairwise,
                dim_hidden=outer_product_mean_dim_hidden,
            )

            msa_pair_weighted_avg = MSAPairWeightedAveraging(
                dim_msa=dim_msa,
                dim_pairwise=dim_pairwise,
                heads=msa_pwa_heads,
                dim_head=msa_pwa_dim_head,
                dropout=msa_pwa_dropout_row_prob,
                dropout_type="row",
            )

            msa_transition = Transition(dim=dim_msa)

            pairwise_block = PairwiseBlock(dim_pairwise=dim_pairwise, **pairwise_block_kwargs)

            layers.append(
                ModuleList(
                    [
                        outer_product_mean,
                        msa_pair_weighted_avg,
                        msa_pre_ln(msa_transition),
                        pairwise_block,
                    ]
                )
            )

        self.layers = layers

    @typecheck
    def forward(
        self,
        *,
        single_repr: Float["b n ds"],  # type: ignore
        pairwise_repr: Float["b n n dp"],  # type: ignore
        msa: Float["b s n dm"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
        msa_mask: Bool["b s"] | None = None,  # type: ignore
    ) -> Float["b n n dp"]:  # type: ignore
        """
        Perform the forward pass.

        :param single_repr: The single representation tensor.
        :param pairwise_repr: The pairwise representation tensor.
        :param msa: The MSA tensor.
        :param mask: The mask tensor.
        :param msa_mask: The MSA mask tensor.
        :return: The output tensor.
        """
        batch, num_msa, device = *msa.shape[:2], msa.device

        # sample without replacement

        if num_msa > self.max_num_msa:
            rand = torch.randn((batch, num_msa), device=device)

            if exists(msa_mask):
                rand.masked_fill_(~msa_mask, max_neg_value(msa))

            indices = rand.topk(self.max_num_msa, dim=-1).indices

            msa = einx.get_at("b [s] n dm, b sampled -> b sampled n dm", msa, indices)

            if exists(msa_mask):
                msa_mask = einx.get_at("b [s], b sampled -> b sampled", msa_mask, indices)

        # account for no msa

        if exists(msa_mask):
            has_msa = reduce(msa_mask, "b s -> b", "any")

        # process msa

        msa = self.msa_init_proj(msa)

        single_msa_feats = self.single_to_msa_feats(single_repr)

        msa = rearrange(single_msa_feats, "b n d -> b 1 n d") + msa

        for (
            outer_product_mean,
            msa_pair_weighted_avg,
            msa_transition,
            pairwise_block,
        ) in self.layers:
            # communication between msa and pairwise rep

            pairwise_repr = outer_product_mean(msa, mask=mask, msa_mask=msa_mask) + pairwise_repr

            msa = msa_pair_weighted_avg(msa=msa, pairwise_repr=pairwise_repr, mask=mask) + msa
            msa = msa_transition(msa) + msa

            # pairwise block

            pairwise_repr = pairwise_block(pairwise_repr=pairwise_repr, mask=mask)

        if exists(msa_mask):
            pairwise_repr = einx.where("b, b ..., -> b ...", has_msa, pairwise_repr, 0.0)

        return pairwise_repr


# pairformer stack


class PairformerStack(Module):
    """Algorithm 17."""

    def __init__(
        self,
        *,
        dim_single=384,
        dim_pairwise=128,
        depth=48,
        pair_bias_attn_dim_head=64,
        pair_bias_attn_heads=16,
        dropout_row_prob=0.25,
        num_register_tokens=0,
        pairwise_block_kwargs: dict = dict(),
    ):
        super().__init__()
        layers = ModuleList([])

        pair_bias_attn_kwargs = dict(
            dim=dim_single,
            dim_pairwise=dim_pairwise,
            heads=pair_bias_attn_heads,
            dim_head=pair_bias_attn_dim_head,
            dropout=dropout_row_prob,
        )

        for _ in range(depth):
            single_pre_ln = partial(PreLayerNorm, dim=dim_single)

            pairwise_block = PairwiseBlock(dim_pairwise=dim_pairwise, **pairwise_block_kwargs)

            pair_bias_attn = AttentionPairBias(**pair_bias_attn_kwargs)
            single_transition = Transition(dim=dim_single)

            layers.append(
                ModuleList(
                    [
                        pairwise_block,
                        single_pre_ln(pair_bias_attn),
                        single_pre_ln(single_transition),
                    ]
                )
            )

        self.layers = layers

        self.num_registers = num_register_tokens
        self.has_registers = num_register_tokens > 0

        if self.has_registers:
            self.single_registers = nn.Parameter(torch.zeros(num_register_tokens, dim_single))
            self.pairwise_row_registers = nn.Parameter(
                torch.zeros(num_register_tokens, dim_pairwise)
            )
            self.pairwise_col_registers = nn.Parameter(
                torch.zeros(num_register_tokens, dim_pairwise)
            )

    @typecheck
    def forward(
        self,
        *,
        single_repr: Float["b n ds"],  # type: ignore
        pairwise_repr: Float["b n n dp"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
    ) -> Tuple[Float["b n ds"], Float["b n n dp"]]:  # type: ignore
        """
        Perform the forward pass.

        :param single_repr: The single representation tensor.
        :param pairwise_repr: The pairwise representation tensor.
        :param mask: The mask tensor.
        :return: The output tensors.
        """
        # prepend register tokens

        if self.has_registers:
            batch_size, num_registers = (
                single_repr.shape[0],
                self.num_registers,
            )
            single_registers = repeat(self.single_registers, "r d -> b r d", b=batch_size)
            single_repr = torch.cat((single_registers, single_repr), dim=1)

            row_registers = repeat(
                self.pairwise_row_registers,
                "r d -> b r n d",
                b=batch_size,
                n=pairwise_repr.shape[-2],
            )
            pairwise_repr = torch.cat((row_registers, pairwise_repr), dim=1)
            col_registers = repeat(
                self.pairwise_col_registers,
                "r d -> b n r d",
                b=batch_size,
                n=pairwise_repr.shape[1],
            )
            pairwise_repr = torch.cat((col_registers, pairwise_repr), dim=2)

            if exists(mask):
                mask = F.pad(mask, (num_registers, 0), value=True)

        # main transformer block layers

        for pairwise_block, pair_bias_attn, single_transition in self.layers:
            pairwise_repr = pairwise_block(pairwise_repr=pairwise_repr, mask=mask)

            single_repr = (
                pair_bias_attn(single_repr, pairwise_repr=pairwise_repr, mask=mask) + single_repr
            )
            single_repr = single_transition(single_repr) + single_repr

        # splice out registers

        if self.has_registers:
            single_repr = single_repr[:, num_registers:]
            pairwise_repr = pairwise_repr[:, num_registers:, num_registers:]

        return single_repr, pairwise_repr


# embedding related


class RelativePositionEncoding(Module):
    """Algorithm 3."""

    def __init__(self, *, r_max=32, s_max=2, dim_out=128):
        super().__init__()
        self.r_max = r_max
        self.s_max = s_max

        dim_input = (2 * r_max + 2) + (2 * r_max + 2) + 1 + (2 * s_max + 2)
        self.out_embedder = LinearNoBias(dim_input, dim_out)

    @typecheck
    def forward(
        self, *, additional_residue_feats: Float[f"b n {ADDITIONAL_RESIDUE_FEATS}"]  # type: ignore
    ) -> Float["b n n dp"]:  # type: ignore
        """
        Perform the forward pass.

        :param additional_residue_feats: The additional residue features tensor.
        :return: The output tensor.
        """

        device = additional_residue_feats.device
        assert (
            additional_residue_feats.shape[-1] >= 5
        ), "Additional residue features must have at least 5 dimensions."

        (
            res_idx,
            token_idx,
            asym_id,
            entity_id,
            sym_id,
        ) = additional_residue_feats[
            ..., :5
        ].unbind(dim=-1)

        diff_res_idx = einx.subtract("b i, b j -> b i j", res_idx, res_idx)
        diff_token_idx = einx.subtract("b i, b j -> b i j", token_idx, token_idx)
        diff_sym_id = einx.subtract("b i, b j -> b i j", sym_id, sym_id)

        mask_same_chain = einx.subtract("b i, b j -> b i j", asym_id, asym_id) == 0
        mask_same_res = diff_res_idx == 0
        mask_same_entity = einx.subtract("b i, b j -> b i j 1", entity_id, entity_id) == 0

        d_res = torch.where(
            mask_same_chain,
            torch.clip(diff_res_idx + self.r_max, 0, 2 * self.r_max),
            2 * self.r_max + 1,
        )

        d_token = torch.where(
            mask_same_chain * mask_same_res,
            torch.clip(diff_token_idx + self.r_max, 0, 2 * self.r_max),
            2 * self.r_max + 1,
        )

        d_chain = torch.where(
            ~mask_same_chain,
            torch.clip(diff_sym_id + self.s_max, 0, 2 * self.s_max),
            2 * self.s_max + 1,
        )

        def onehot(x, bins):
            dist_from_bins = einx.subtract("... i, j -> ... i j", x, bins)
            indices = dist_from_bins.abs().min(dim=-1, keepdim=True).indices
            one_hots = F.one_hot(indices.long(), num_classes=len(bins))
            return one_hots.float()

        r_arange = torch.arange(2 * self.r_max + 2, device=device)
        s_arange = torch.arange(2 * self.s_max + 2, device=device)

        a_rel_pos = onehot(d_res, r_arange)
        a_rel_token = onehot(d_token, r_arange)
        a_rel_chain = onehot(d_chain, s_arange)

        out, _ = pack((a_rel_pos, a_rel_token, mask_same_entity, a_rel_chain), "b i j *")

        return self.out_embedder(out)


class TemplateEmbedder(Module):
    """Algorithm 16."""

    def __init__(
        self,
        *,
        dim_template_feats,
        dim=64,
        dim_pairwise=128,
        pairformer_stack_depth=2,
        pairwise_block_kwargs: dict = dict(),
        eps=1e-5,
    ):
        super().__init__()
        self.eps = eps

        self.template_feats_to_embed_input = LinearNoBias(dim_template_feats, dim)

        self.pairwise_to_embed_input = nn.Sequential(
            nn.LayerNorm(dim_pairwise), LinearNoBias(dim_pairwise, dim)
        )

        layers = ModuleList([])
        for _ in range(pairformer_stack_depth):
            block = PairwiseBlock(dim_pairwise=dim, **pairwise_block_kwargs)

            layers.append(block)

        self.pairformer_stack = layers

        self.final_norm = nn.LayerNorm(dim)

        # final projection of mean pooled repr -> out

        self.to_out = nn.Sequential(LinearNoBias(dim, dim_pairwise), nn.ReLU())

    @typecheck
    def forward(
        self,
        *,
        templates: Float["b t n n dt"],  # type: ignore
        template_mask: Bool["b t"],  # type: ignore
        pairwise_repr: Float["b n n dp"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
    ) -> Float["b n n dp"]:  # type: ignore
        """
        Perform the forward pass.

        :param templates: The templates tensor.
        :param template_mask: The template mask tensor.
        :param pairwise_repr: The pairwise representation tensor.
        :param mask: The mask tensor.
        :return: The output tensor.
        """

        num_templates = templates.shape[1]

        pairwise_repr = self.pairwise_to_embed_input(pairwise_repr)
        pairwise_repr = rearrange(pairwise_repr, "b i j d -> b 1 i j d")

        v = self.template_feats_to_embed_input(templates) + pairwise_repr

        v, merged_batch_ps = pack_one(v, "* i j d")

        has_templates = reduce(template_mask, "b t -> b", "any")

        if exists(mask):
            mask = repeat(mask, "b n -> (b t) n", t=num_templates)

        for block in self.pairformer_stack:
            v = block(pairwise_repr=v, mask=mask) + v

        u = self.final_norm(v)

        u = unpack_one(u, merged_batch_ps, "* i jk d")

        # masked mean pool template repr

        u = einx.where("b t, b t ..., -> b t ...", template_mask, u, 0.0)

        num = reduce(u, "b t i j d -> b i j d", "sum")
        den = reduce(template_mask.float(), "b t -> b", "sum")

        avg_template_repr = einx.divide("b i j d, b -> b i j d", num, den.clamp(min=self.eps))

        out = self.to_out(avg_template_repr)

        out = einx.where("b, b ..., -> b ...", has_templates, out, 0.0)

        return out


# diffusion related
# both diffusion transformer as well as atom encoder / decoder


class FourierEmbedding(Module):
    """Algorithm 22."""

    def __init__(self, dim):
        super().__init__()
        self.proj = nn.Linear(1, dim)
        self.proj.requires_grad_(False)

    @typecheck
    def forward(
        self,
        times: Float[" b"],  # type: ignore
    ) -> Float["b d"]:  # type: ignore
        """
        Perform the forward pass.

        :param times: The times tensor.
        :return: The output tensor.
        """
        times = rearrange(times, "b -> b 1")
        rand_proj = self.proj(times)
        return torch.cos(2 * pi * rand_proj)


class PairwiseConditioning(Module):
    """Algorithm 21."""

    def __init__(
        self,
        *,
        dim_pairwise_trunk,
        dim_pairwise_rel_pos_feats,
        dim_pairwise=128,
        num_transitions=2,
        transition_expansion_factor=2,
    ):
        super().__init__()

        self.dim_pairwise_init_proj = nn.Sequential(
            LinearNoBias(dim_pairwise_trunk + dim_pairwise_rel_pos_feats, dim_pairwise),
            nn.LayerNorm(dim_pairwise),
        )

        transitions = ModuleList([])
        for _ in range(num_transitions):
            transition = PreLayerNorm(
                Transition(
                    dim=dim_pairwise,
                    expansion_factor=transition_expansion_factor,
                ),
                dim=dim_pairwise,
            )
            transitions.append(transition)

        self.transitions = transitions

    @typecheck
    def forward(
        self,
        *,
        pairwise_trunk: Float["b n n dpt"],  # type: ignore
        pairwise_rel_pos_feats: Float["b n n dpr"],  # type: ignore
    ) -> Float["b n n dp"]:  # type: ignore
        """
        Perform the forward pass.

        :param pairwise_trunk: The pairwise trunk tensor.
        :param pairwise_rel_pos_feats: The pairwise relative position features tensor.
        :return: The output tensor.
        """
        pairwise_repr = torch.cat((pairwise_trunk, pairwise_rel_pos_feats), dim=-1)

        pairwise_repr = self.dim_pairwise_init_proj(pairwise_repr)

        for transition in self.transitions:
            pairwise_repr = transition(pairwise_repr) + pairwise_repr

        return pairwise_repr


class SingleConditioning(Module):
    """Algorithm 21."""

    def __init__(
        self,
        *,
        sigma_data: float,
        dim_single=384,
        dim_fourier=256,
        num_transitions=2,
        transition_expansion_factor=2,
        eps=1e-20,
    ):
        super().__init__()
        self.eps = eps

        self.dim_single = dim_single
        self.sigma_data = sigma_data

        self.norm_single = nn.LayerNorm(dim_single)

        self.fourier_embed = FourierEmbedding(dim_fourier)
        self.norm_fourier = nn.LayerNorm(dim_fourier)
        self.fourier_to_single = LinearNoBias(dim_fourier, dim_single)

        transitions = ModuleList([])
        for _ in range(num_transitions):
            transition = PreLayerNorm(
                Transition(
                    dim=dim_single,
                    expansion_factor=transition_expansion_factor,
                ),
                dim=dim_single,
            )
            transitions.append(transition)

        self.transitions = transitions

    @typecheck
    def forward(
        self,
        *,
        times: Float[" b"],  # type: ignore
        single_trunk_repr: Float["b n dst"],  # type: ignore
        single_inputs_repr: Float["b n dsi"],  # type: ignore
    ) -> Float["b n (dst+dsi)"]:  # type: ignore
        """
        Perform the forward pass.

        :param times: The times tensor.
        :param single_trunk_repr: The single trunk representation tensor.
        :param single_inputs_repr: The single inputs representation tensor.
        :return: The output tensor.
        """
        single_repr = torch.cat((single_trunk_repr, single_inputs_repr), dim=-1)

        assert (
            single_repr.shape[-1] == self.dim_single
        ), "Single representation must have the correct dimension."

        single_repr = self.norm_single(single_repr)

        fourier_embed = self.fourier_embed(0.25 * log(times / self.sigma_data, eps=self.eps))

        normed_fourier = self.norm_fourier(fourier_embed)

        fourier_to_single = self.fourier_to_single(normed_fourier)

        single_repr = rearrange(fourier_to_single, "b d -> b 1 d") + single_repr

        for transition in self.transitions:
            single_repr = transition(single_repr) + single_repr

        return single_repr


class DiffusionTransformer(Module):
    """Algorithm 23."""

    def __init__(
        self,
        *,
        depth,
        heads,
        dim=384,
        dim_single_cond=None,
        dim_pairwise=128,
        attn_window_size=None,
        attn_pair_bias_kwargs: dict = dict(),
        num_register_tokens=0,
        serial=False,
        use_linear_attn=False,
        linear_attn_kwargs=dict(heads=8, dim_head=16),
    ):
        super().__init__()
        self.attn_window_size = attn_window_size

        dim_single_cond = default(dim_single_cond, dim)

        layers = ModuleList([])

        for _ in range(depth):
            linear_attn = None

            if use_linear_attn:
                linear_attn = TaylorSeriesLinearAttn(
                    dim=dim,
                    prenorm=True,
                    gate_value_heads=True,
                    **linear_attn_kwargs,
                )

            pair_bias_attn = AttentionPairBias(
                dim=dim,
                dim_pairwise=dim_pairwise,
                heads=heads,
                window_size=attn_window_size,
                **attn_pair_bias_kwargs,
            )

            transition = Transition(dim=dim)

            conditionable_pair_bias = ConditionWrapper(
                pair_bias_attn, dim=dim, dim_cond=dim_single_cond
            )

            conditionable_transition = ConditionWrapper(
                transition, dim=dim, dim_cond=dim_single_cond
            )

            layers.append(
                ModuleList([linear_attn, conditionable_pair_bias, conditionable_transition])
            )

        self.layers = layers

        self.serial = serial

        self.has_registers = num_register_tokens > 0
        self.num_registers = num_register_tokens

        if self.has_registers:
            assert not exists(
                attn_window_size
            ), "Register tokens are disabled for windowed attention."
            self.registers = nn.Parameter(torch.zeros(num_register_tokens, dim))

    @typecheck
    def forward(
        self,
        noised_repr: Float["b n d"],  # type: ignore
        *,
        single_repr: Float["b n ds"],  # type: ignore
        pairwise_repr: Float["b n n dp"] | Float["b nw w (w*3) dp"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
    ):
        """
        Perform the forward pass.

        :param noised_repr: The noised representation tensor.
        :param single_repr: The single representation tensor.
        :param pairwise_repr: The pairwise representation tensor.
        :param mask: The mask tensor.
        :return: The output tensor.
        """
        w = self.attn_window_size
        has_windows = exists(w)

        serial = self.serial

        # handle windowing

        pairwise_is_windowed = pairwise_repr.ndim == 5

        if has_windows and not pairwise_is_windowed:
            pairwise_repr = full_pairwise_repr_to_windowed(pairwise_repr, window_size=w)

        # register tokens

        if self.has_registers:
            num_registers = self.num_registers
            registers = repeat(self.registers, "r d -> b r d", b=noised_repr.shape[0])
            noised_repr, registers_ps = pack((registers, noised_repr), "b * d")

            single_repr = F.pad(single_repr, (0, 0, num_registers, 0), value=0.0)
            pairwise_repr = F.pad(
                pairwise_repr,
                (0, 0, num_registers, 0, num_registers, 0),
                value=0.0,
            )

            if exists(mask):
                mask = F.pad(mask, (num_registers, 0), value=True)

        # main transformer

        for linear_attn, attn, transition in self.layers:
            if exists(linear_attn):
                noised_repr = linear_attn(noised_repr, mask=mask) + noised_repr

            attn_out = attn(
                noised_repr,
                cond=single_repr,
                pairwise_repr=pairwise_repr,
                mask=mask,
            )

            if serial:
                noised_repr = attn_out + noised_repr

            ff_out = transition(noised_repr, cond=single_repr)

            if not serial:
                ff_out = ff_out + attn_out

            noised_repr = noised_repr + ff_out

        # splice out registers

        if self.has_registers:
            _, noised_repr = unpack(noised_repr, registers_ps, "b * d")

        return noised_repr


class AtomToTokenPooler(Module):
    """Algorithm 24."""

    def __init__(self, dim, dim_out=None):
        super().__init__()
        dim_out = default(dim_out, dim)

        self.proj = nn.Sequential(LinearNoBias(dim, dim_out), nn.ReLU())

    @typecheck
    def forward(
        self,
        *,
        atom_feats: Float["b m da"],  # type: ignore
        atom_mask: Bool["b m"],  # type: ignore
        residue_atom_lens: Int["b n"],  # type: ignore
    ) -> Float["b n ds"]:  # type: ignore
        """
        Perform the forward pass.

        :param atom_feats: The atom features tensor.
        :param atom_mask: The atom mask tensor.
        :param residue_atom_lens: The residue atom lengths tensor.
        :return: The output tensor.
        """
        atom_feats = self.proj(atom_feats)
        tokens = mean_pool_with_lens(atom_feats, residue_atom_lens)
        return tokens


class DiffusionModule(Module):
    """Algorithm 20."""

    @typecheck
    def __init__(
        self,
        *,
        dim_pairwise_trunk,
        dim_pairwise_rel_pos_feats,
        atoms_per_window=27,  # for atom sequence, take the approach of (batch, seq, atoms, ..), where atom dimension is set to the residue or molecule with greatest number of atoms, the rest padded. atom_mask must be passed in - default to 27 for proteins, with tryptophan having 27 atoms
        dim_pairwise=128,
        sigma_data=16,
        dim_atom=128,
        dim_atompair=16,
        dim_token=768,
        dim_single=384,
        dim_fourier=256,
        single_cond_kwargs: dict = dict(
            num_transitions=2,
            transition_expansion_factor=2,
        ),
        pairwise_cond_kwargs: dict = dict(num_transitions=2),
        atom_encoder_depth=3,
        atom_encoder_heads=4,
        token_transformer_depth=24,
        token_transformer_heads=16,
        atom_decoder_depth=3,
        atom_decoder_heads=4,
        serial=False,
        atom_encoder_kwargs: dict = dict(),
        atom_decoder_kwargs: dict = dict(),
        token_transformer_kwargs: dict = dict(),
        use_linear_attn=False,
        linear_attn_kwargs: dict = dict(heads=8, dim_head=16),
    ):
        super().__init__()

        self.atoms_per_window = atoms_per_window

        # conditioning

        self.single_conditioner = SingleConditioning(
            sigma_data=sigma_data,
            dim_single=dim_single,
            dim_fourier=dim_fourier,
            **single_cond_kwargs,
        )

        self.pairwise_conditioner = PairwiseConditioning(
            dim_pairwise_trunk=dim_pairwise_trunk,
            dim_pairwise_rel_pos_feats=dim_pairwise_rel_pos_feats,
            **pairwise_cond_kwargs,
        )

        # atom attention encoding related modules

        self.atom_pos_to_atom_feat = LinearNoBias(3, dim_atom)

        self.single_repr_to_atom_feat_cond = nn.Sequential(
            nn.LayerNorm(dim_single), LinearNoBias(dim_single, dim_atom)
        )

        self.pairwise_repr_to_atompair_feat_cond = nn.Sequential(
            nn.LayerNorm(dim_pairwise),
            LinearNoBias(dim_pairwise, dim_atompair),
        )

        self.atom_repr_to_atompair_feat_cond = nn.Sequential(
            nn.LayerNorm(dim_atom),
            LinearNoBias(dim_atom, dim_atompair * 2),
            nn.ReLU(),
        )

        self.atompair_feats_mlp = nn.Sequential(
            LinearNoBias(dim_atompair, dim_atompair),
            nn.ReLU(),
            LinearNoBias(dim_atompair, dim_atompair),
            nn.ReLU(),
            LinearNoBias(dim_atompair, dim_atompair),
        )

        self.atom_encoder = DiffusionTransformer(
            dim=dim_atom,
            dim_single_cond=dim_atom,
            dim_pairwise=dim_atompair,
            attn_window_size=atoms_per_window,
            depth=atom_encoder_depth,
            heads=atom_encoder_heads,
            serial=serial,
            use_linear_attn=use_linear_attn,
            linear_attn_kwargs=linear_attn_kwargs,
            **atom_encoder_kwargs,
        )

        self.atom_feats_to_pooled_token = AtomToTokenPooler(
            dim=dim_atom,
            dim_out=dim_token,
        )

        # token attention related modules

        self.cond_tokens_with_cond_single = nn.Sequential(
            nn.LayerNorm(dim_single), LinearNoBias(dim_single, dim_token)
        )

        self.token_transformer = DiffusionTransformer(
            dim=dim_token,
            dim_single_cond=dim_single,
            dim_pairwise=dim_pairwise,
            depth=token_transformer_depth,
            heads=token_transformer_heads,
            serial=serial,
            **token_transformer_kwargs,
        )

        self.attended_token_norm = nn.LayerNorm(dim_token)

        # atom attention decoding related modules

        self.tokens_to_atom_decoder_input_cond = LinearNoBias(dim_token, dim_atom)

        self.atom_decoder = DiffusionTransformer(
            dim=dim_atom,
            dim_single_cond=dim_atom,
            dim_pairwise=dim_atompair,
            attn_window_size=atoms_per_window,
            depth=atom_decoder_depth,
            heads=atom_decoder_heads,
            serial=serial,
            use_linear_attn=use_linear_attn,
            linear_attn_kwargs=linear_attn_kwargs,
            **atom_decoder_kwargs,
        )

        self.atom_feat_to_atom_pos_update = nn.Sequential(
            nn.LayerNorm(dim_atom), LinearNoBias(dim_atom, 3)
        )

    @typecheck
    def forward(
        self,
        noised_atom_pos: Float["b m 3"],  # type: ignore
        *,
        atom_feats: Float["b m da"],  # type: ignore
        atompair_feats: Float["b m m dap"] | Float["b nw w (w*3) dap"],  # type: ignore
        atom_mask: Bool["b m"],  # type: ignore
        times: Float[" b"],  # type: ignore
        mask: Bool["b n"],  # type: ignore
        single_trunk_repr: Float["b n dst"],  # type: ignore
        single_inputs_repr: Float["b n dsi"],  # type: ignore
        pairwise_trunk: Float["b n n dpt"],  # type: ignore
        pairwise_rel_pos_feats: Float["b n n dpr"],  # type: ignore
        residue_atom_lens: Int["b n"],  # type: ignore
    ) -> Float["b m 3"]:  # type: ignore
        """
        Perform the forward pass.

        :param noised_atom_pos: The noised atom position tensor.
        :param atom_feats: The atom features tensor.
        :param atompair_feats: The atom pair features tensor.
        :param atom_mask: The atom mask tensor.
        :param times: The times tensor.
        :param mask: The mask tensor.
        :param single_trunk_repr: The single trunk representation tensor.
        :param single_inputs_repr: The single inputs representation tensor.
        :param pairwise_trunk: The pairwise trunk tensor.
        :param pairwise_rel_pos_feats: The pairwise relative position features tensor.
        :param residue_atom_lens: The residue atom lengths tensor.
        :return: The output tensor.
        """
        w = self.atoms_per_window
        device = noised_atom_pos.device

        batch_size, seq_len = single_trunk_repr.shape[:2]
        atom_seq_len = atom_feats.shape[1]

        conditioned_single_repr = self.single_conditioner(
            times=times,
            single_trunk_repr=single_trunk_repr,
            single_inputs_repr=single_inputs_repr,
        )

        conditioned_pairwise_repr = self.pairwise_conditioner(
            pairwise_trunk=pairwise_trunk,
            pairwise_rel_pos_feats=pairwise_rel_pos_feats,
        )

        # lines 7-14 in Algorithm 5

        atom_feats_cond = atom_feats

        # the most surprising part of the paper; no geometric biases!

        atom_feats = self.atom_pos_to_atom_feat(noised_atom_pos) + atom_feats

        # condition atom feats cond (cl) with single repr

        single_repr_cond = self.single_repr_to_atom_feat_cond(conditioned_single_repr)

        single_repr_cond = repeat_consecutive_with_lens(single_repr_cond, residue_atom_lens)
        single_repr_cond = pad_or_slice_to(
            single_repr_cond, length=atom_feats_cond.shape[1], dim=1
        )

        atom_feats_cond = single_repr_cond + atom_feats_cond

        # window the atom pair features before passing to atom encoder and decoder if necessary

        atompair_is_windowed = atompair_feats.ndim == 5

        if not atompair_is_windowed:
            atompair_feats = full_pairwise_repr_to_windowed(
                atompair_feats, window_size=self.atoms_per_window
            )

        # condition atompair feats with pairwise repr

        pairwise_repr_cond = self.pairwise_repr_to_atompair_feat_cond(conditioned_pairwise_repr)

        indices = torch.arange(seq_len, device=device)
        indices = repeat(indices, "n -> b n", b=batch_size)

        indices = repeat_consecutive_with_lens(indices, residue_atom_lens)
        indices = pad_or_slice_to(indices, atom_seq_len, dim=-1)
        indices = pad_and_window(indices, w)

        row_indices = col_indices = indices
        row_indices = rearrange(row_indices, "b n w -> b n w 1", w=w)
        col_indices = rearrange(col_indices, "b n w -> b n 1 w", w=w)

        col_indices = concat_neighboring_windows(col_indices, dim_seq=1, dim_window=-1)
        row_indices, col_indices = torch.broadcast_tensors(row_indices, col_indices)

        pairwise_repr_cond = einx.get_at(
            "b [i j] dap, b nw w1 w2, b nw w1 w2 -> b nw w1 w2 dap",
            pairwise_repr_cond,
            row_indices,
            col_indices,
        )

        atompair_feats = pairwise_repr_cond + atompair_feats

        # condition atompair feats further with single atom repr

        atom_repr_cond = self.atom_repr_to_atompair_feat_cond(atom_feats)
        atom_repr_cond = pad_and_window(atom_repr_cond, w)

        atom_repr_cond_row, atom_repr_cond_col = atom_repr_cond.chunk(2, dim=-1)

        atom_repr_cond_col = concat_neighboring_windows(
            atom_repr_cond_col, dim_seq=1, dim_window=2
        )

        atompair_feats = einx.add(
            "b nw w1 w2 dap, b nw w1 dap -> b nw w1 w2 dap", atompair_feats, atom_repr_cond_row
        )
        atompair_feats = einx.add(
            "b nw w1 w2 dap, b nw w2 dap -> b nw w1 w2 dap", atompair_feats, atom_repr_cond_col
        )

        # furthermore, they did one more MLP on the atompair feats for attention biasing in atom transformer

        atompair_feats = self.atompair_feats_mlp(atompair_feats) + atompair_feats

        # atom encoder

        atom_feats = self.atom_encoder(
            atom_feats,
            mask=atom_mask,
            single_repr=atom_feats_cond,
            pairwise_repr=atompair_feats,
        )

        atom_feats_skip = atom_feats

        tokens = self.atom_feats_to_pooled_token(
            atom_feats=atom_feats,
            atom_mask=atom_mask,
            residue_atom_lens=residue_atom_lens,
        )

        # token transformer

        tokens = self.cond_tokens_with_cond_single(conditioned_single_repr) + tokens

        self.token_transformer(
            tokens,
            mask=mask,
            single_repr=conditioned_single_repr,
            pairwise_repr=conditioned_pairwise_repr,
        )

        tokens = self.attended_token_norm(tokens)

        # atom decoder

        atom_decoder_input = self.tokens_to_atom_decoder_input_cond(tokens)

        atom_decoder_input = repeat_consecutive_with_lens(atom_decoder_input, residue_atom_lens)
        atom_decoder_input = pad_or_slice_to(
            atom_decoder_input, length=atom_feats_skip.shape[1], dim=1
        )

        atom_decoder_input = atom_decoder_input + atom_feats_skip

        atom_feats = self.atom_decoder(
            atom_decoder_input,
            mask=atom_mask,
            single_repr=atom_feats_cond,
            pairwise_repr=atompair_feats,
        )

        atom_pos_update = self.atom_feat_to_atom_pos_update(atom_feats)

        return atom_pos_update


# elucidated diffusion model adapted for atom position diffusing
# from Karras et al.
# https://arxiv.org/abs/2206.00364


class DiffusionLossBreakdown(NamedTuple):
    """The DiffusionLossBreakdown class."""

    diffusion_mse: Float[""]  # type: ignore
    diffusion_bond: Float[""]  # type: ignore
    diffusion_smooth_lddt: Float[""]  # type: ignore


class ElucidatedAtomDiffusionReturn(NamedTuple):
    """The ElucidatedAtomDiffusionReturn class."""

    loss: Float[""]  # type: ignore
    denoised_atom_pos: Float["ba m 3"]  # type: ignore
    loss_breakdown: DiffusionLossBreakdown
    noise_sigmas: Float[" ba"]  # type: ignore


class ElucidatedAtomDiffusion(Module):
    """An ElucidatedAtomDiffusion module."""

    @typecheck
    def __init__(
        self,
        net: DiffusionModule,
        *,
        num_sample_steps=32,
        sigma_min=0.002,
        sigma_max=80,
        sigma_data=0.5,
        rho=7,
        P_mean=-1.2,
        P_std=1.2,
        S_churn=80,
        S_tmin=0.05,
        S_tmax=50,
        S_noise=1.003,
        smooth_lddt_loss_kwargs: dict = dict(),
        weighted_rigid_align_kwargs: dict = dict(),
    ):  # number of sampling steps  # min noise level  # max noise level  # standard deviation of data distribution  # controls the sampling schedule  # mean of log-normal distribution from which noise is drawn for training  # standard deviation of log-normal distribution from which noise is drawn for training  # parameters for stochastic sampling - depends on dataset, Table 5 in apper
        super().__init__()
        self.net = net

        # parameters

        self.sigma_min = sigma_min
        self.sigma_max = sigma_max
        self.sigma_data = sigma_data

        self.rho = rho

        self.P_mean = P_mean
        self.P_std = P_std

        self.num_sample_steps = num_sample_steps  # otherwise known as N in the paper

        self.S_churn = S_churn
        self.S_tmin = S_tmin
        self.S_tmax = S_tmax
        self.S_noise = S_noise

        # weighted rigid align

        self.weighted_rigid_align = WeightedRigidAlign(**weighted_rigid_align_kwargs)

        # smooth lddt loss

        self.smooth_lddt_loss = SmoothLDDTLoss(**smooth_lddt_loss_kwargs)

        self.register_buffer("zero", torch.tensor(0.0), persistent=False)

    @property
    def device(self):
        """
        Return the device of the module.

        :return: The device of the module.
        """
        return next(self.net.parameters()).device

    # derived preconditioning params - Table 1

    def c_skip(self, sigma):
        """
        Return the c_skip value.

        :param sigma: The sigma value.
        :return: The c_skip value.
        """
        return (self.sigma_data**2) / (sigma**2 + self.sigma_data**2)

    def c_out(self, sigma):
        """
        Return the c_out value.

        :param sigma: The sigma value.
        :return: The c_out value.
        """
        return sigma * self.sigma_data * (self.sigma_data**2 + sigma**2) ** -0.5

    def c_in(self, sigma):
        """
        Return the c_in value.

        :param sigma: The sigma value.
        :return: The c_in value.
        """
        return 1 * (sigma**2 + self.sigma_data**2) ** -0.5

    def c_noise(self, sigma):
        """
        Return the c_noise value.

        :param sigma: The sigma value.
        :return: The c_noise value.
        """
        return log(sigma) * 0.25

    # preconditioned network output

    @typecheck
    def preconditioned_network_forward(
        self,
        noised_atom_pos: Float["b m 3"],  # type: ignore
        sigma: Float[" b"] | Float[" "] | float,  # type: ignore
        network_condition_kwargs: dict,
        clamp=False,
    ):
        """
        Run a network forward pass, with the preconditioned inputs.

        :param noised_atom_pos: The noised atom position tensor.
        :param sigma: The sigma value.
        :param network_condition_kwargs: The network condition keyword arguments.
        :param clamp: Whether to clamp the output.
        :return: The output tensor.
        """
        batch, device = noised_atom_pos.shape[0], noised_atom_pos.device

        if isinstance(sigma, float):
            sigma = torch.full((batch,), sigma, device=device)

        padded_sigma = rearrange(sigma, "b -> b 1 1")

        net_out = self.net(
            self.c_in(padded_sigma) * noised_atom_pos,
            times=self.c_noise(sigma),
            **network_condition_kwargs,
        )

        out = self.c_skip(padded_sigma) * noised_atom_pos + self.c_out(padded_sigma) * net_out

        if clamp:
            out = out.clamp(-1.0, 1.0)

        return out

    # sampling

    # sample schedule
    # equation (7) in the paper

    def sample_schedule(self, num_sample_steps=None):
        """
        Return the schedule of sigmas for sampling.
        Algorithm (7) in the paper.

        :param num_sample_steps: The number of sample steps.
        :return: The schedule of sigmas for sampling.
        """
        num_sample_steps = default(num_sample_steps, self.num_sample_steps)

        N = num_sample_steps
        inv_rho = 1 / self.rho

        # NOTE: this differs in notation from the paper slightly
        steps = torch.arange(num_sample_steps, device=self.device, dtype=torch.float32)
        sigmas = (
            self.sigma_max**inv_rho
            + steps / (N - 1) * (self.sigma_min**inv_rho - self.sigma_max**inv_rho)
        ) ** self.rho

        sigmas = F.pad(sigmas, (0, 1), value=0.0)  # last step is sigma value of 0.
        return sigmas

    @torch.no_grad()
    def sample(self, atom_mask: Bool["b m"] | None = None, num_sample_steps=None, clamp=False, **network_condition_kwargs):  # type: ignore
        """
        Sample clean atom positions.

        :param atom_mask: The atom mask tensor.
        :param num_sample_steps: The number of sample steps.
        :param clamp: Whether to clamp the output.
        :param network_condition_kwargs: The network condition keyword arguments.
        :return: The atom positions.
        """
        num_sample_steps = default(num_sample_steps, self.num_sample_steps)

        shape = (*atom_mask.shape, 3)

        network_condition_kwargs.update(atom_mask=atom_mask)

        # get the schedule, which is returned as (sigma, gamma) tuple, and pair up with the next sigma and gamma

        sigmas = self.sample_schedule(num_sample_steps)

        gammas = torch.where(
            (sigmas >= self.S_tmin) & (sigmas <= self.S_tmax),
            min(self.S_churn / num_sample_steps, sqrt(2) - 1),
            0.0,
        )

        sigmas_and_gammas = list(zip(sigmas[:-1], sigmas[1:], gammas[:-1]))

        # atom position is noise at the beginning

        init_sigma = sigmas[0]

        atom_pos = init_sigma * torch.randn(shape, device=self.device)

        # gradually denoise

        for sigma, sigma_next, gamma in tqdm(sigmas_and_gammas, desc="sampling time step"):
            sigma, sigma_next, gamma = map(lambda t: t.item(), (sigma, sigma_next, gamma))

            eps = self.S_noise * torch.randn(shape, device=self.device)  # stochastic sampling

            sigma_hat = sigma + gamma * sigma
            atom_pos_hat = atom_pos + sqrt(sigma_hat**2 - sigma**2) * eps

            model_output = self.preconditioned_network_forward(
                atom_pos_hat,
                sigma_hat,
                clamp=clamp,
                network_condition_kwargs=network_condition_kwargs,
            )
            denoised_over_sigma = (atom_pos_hat - model_output) / sigma_hat

            atom_pos_next = atom_pos_hat + (sigma_next - sigma_hat) * denoised_over_sigma

            # second order correction, if not the last timestep

            if sigma_next != 0:
                model_output_next = self.preconditioned_network_forward(
                    atom_pos_next,
                    sigma_next,
                    clamp=clamp,
                    network_condition_kwargs=network_condition_kwargs,
                )
                denoised_prime_over_sigma = (atom_pos_next - model_output_next) / sigma_next
                atom_pos_next = atom_pos_hat + 0.5 * (sigma_next - sigma_hat) * (
                    denoised_over_sigma + denoised_prime_over_sigma
                )

            atom_pos = atom_pos_next

        if clamp:
            atom_pos = atom_pos.clamp(-1.0, 1.0)

        return atom_pos

    # training

    def loss_weight(self, sigma):
        """
        Return the loss weight for training.

        :param sigma: The sigma value.
        :return: The loss weight for training.
        """
        return (sigma**2 + self.sigma_data**2) * (sigma * self.sigma_data) ** -2

    def noise_distribution(self, batch_size):
        """
        Sample Gaussian-distributed noise.

        :param batch_size: The batch size.
        :return: Sampled Gaussian noise.
        """
        return (self.P_mean + self.P_std * torch.randn((batch_size,), device=self.device)).exp()

    def forward(
        self,
        atom_pos_ground_truth: Float["b m 3"],  # type: ignore
        atom_mask: Bool["b m"],  # type: ignore
        atom_feats: Float["b m da"],  # type: ignore
        atompair_feats: Float["b m m dap"],  # type: ignore
        mask: Bool["b n"],  # type: ignore
        single_trunk_repr: Float["b n dst"],  # type: ignore
        single_inputs_repr: Float["b n dsi"],  # type: ignore
        pairwise_trunk: Float["b n n dpt"],  # type: ignore
        pairwise_rel_pos_feats: Float["b n n dpr"],  # type: ignore
        residue_atom_lens: Int["b n"],  # type: ignore
        return_denoised_pos=False,
        additional_residue_feats: Float[f"b n {ADDITIONAL_RESIDUE_FEATS}"] | None = None,  # type: ignore
        add_smooth_lddt_loss=False,
        add_bond_loss=False,
        nucleotide_loss_weight=5.0,
        ligand_loss_weight=10.0,
        return_loss_breakdown=False,
    ) -> ElucidatedAtomDiffusionReturn:
        """
        Perform the forward pass.

        :param atom_pos_ground_truth: The ground truth atom position tensor.
        :param atom_mask: The atom mask tensor.
        :param atom_feats: The atom features tensor.
        :param atompair_feats: The atom pair features tensor.
        :param mask: The mask tensor.
        :param single_trunk_repr: The single trunk representation tensor.
        :param single_inputs_repr: The single inputs representation tensor.
        :param pairwise_trunk: The pairwise trunk tensor.
        :param pairwise_rel_pos_feats: The pairwise relative position features tensor.
        :param return_denoised_pos: Whether to return the denoised position.
        :param residue_atom_lens: The residue atom lengths tensor.
        :param additional_residue_feats: The additional residue features tensor.
        :param add_smooth_lddt_loss: Whether to add the smooth lddt loss.
        :param add_bond_loss: Whether to add the bond loss.
        :param nucleotide_loss_weight: The nucleotide loss weight.
        :param ligand_loss_weight: The ligand loss weight.
        :param return_loss_breakdown: Whether to return the loss breakdown.
        :return: The output tensor.
        """

        # diffusion loss

        batch_size = atom_pos_ground_truth.shape[0]

        sigmas = self.noise_distribution(batch_size)
        padded_sigmas = rearrange(sigmas, "b -> b 1 1")

        noise = torch.randn_like(atom_pos_ground_truth)

        noised_atom_pos = (
            atom_pos_ground_truth + padded_sigmas * noise
        )  # alphas are 1. in the paper

        denoised_atom_pos = self.preconditioned_network_forward(
            noised_atom_pos,
            sigmas,
            network_condition_kwargs=dict(
                atom_feats=atom_feats,
                atom_mask=atom_mask,
                atompair_feats=atompair_feats,
                mask=mask,
                single_trunk_repr=single_trunk_repr,
                single_inputs_repr=single_inputs_repr,
                pairwise_trunk=pairwise_trunk,
                pairwise_rel_pos_feats=pairwise_rel_pos_feats,
                residue_atom_lens=residue_atom_lens,
            ),
        )

        total_loss = 0.0

        # if additional residue feats is provided
        # calculate the weights for mse loss (wl)

        align_weights = atom_pos_ground_truth.new_ones(atom_pos_ground_truth.shape[:2])

        if exists(additional_residue_feats):
            is_nucleotide_or_ligand_fields = (additional_residue_feats[..., 7:] != 0.0).unbind(
                dim=-1
            )

            is_nucleotide_or_ligand_fields = tuple(
                repeat_consecutive_with_lens(t, residue_atom_lens)
                for t in is_nucleotide_or_ligand_fields
            )
            is_nucleotide_or_ligand_fields = tuple(
                pad_or_slice_to(t, length=align_weights.shape[-1], dim=-1)
                for t in is_nucleotide_or_ligand_fields
            )

            atom_is_dna, atom_is_rna, atom_is_ligand = is_nucleotide_or_ligand_fields

            # section 3.7.1 equation 4

            # upweighting of nucleotide and ligand atoms is additive per equation 4

            align_weights = torch.where(
                atom_is_dna | atom_is_rna,
                1 + nucleotide_loss_weight,
                align_weights,
            )
            align_weights = torch.where(atom_is_ligand, 1 + ligand_loss_weight, align_weights)

        # section 3.7.1 equation 2 - weighted rigid aligned ground truth

        atom_pos_aligned_ground_truth = self.weighted_rigid_align(
            atom_pos_ground_truth,
            denoised_atom_pos,
            align_weights,
            mask=atom_mask,
        )

        # main diffusion mse loss

        losses = (
            F.mse_loss(
                denoised_atom_pos,
                atom_pos_aligned_ground_truth,
                reduction="none",
            )
            / 3.0
        )
        losses = einx.multiply("b m c, b m -> b m c", losses, align_weights)

        # regular loss weight as defined in EDM paper

        loss_weights = self.loss_weight(padded_sigmas)

        losses = losses * loss_weights

        # account for atom mask

        mse_loss = losses[atom_mask].mean()

        total_loss = total_loss + mse_loss

        # proposed extra bond loss during finetuning

        bond_loss = self.zero

        if add_bond_loss:
            atompair_mask = einx.logical_and("b i, b j -> b i j", atom_mask, atom_mask)

            denoised_cdist = torch.cdist(denoised_atom_pos, denoised_atom_pos, p=2)
            normalized_cdist = torch.cdist(atom_pos_ground_truth, atom_pos_ground_truth, p=2)

            bond_losses = F.mse_loss(denoised_cdist, normalized_cdist, reduction="none")
            bond_losses = bond_losses * loss_weights

            bond_loss = bond_losses[atompair_mask].mean()

            total_loss = total_loss + bond_loss

        # proposed auxiliary smooth lddt loss

        smooth_lddt_loss = self.zero

        if add_smooth_lddt_loss:
            assert exists(
                additional_residue_feats
            ), "The argument `additional_residue_feats` must be passed in if adding the smooth lDDT loss."

            smooth_lddt_loss = self.smooth_lddt_loss(
                denoised_atom_pos,
                atom_pos_ground_truth,
                atom_is_dna,
                atom_is_rna,
                coords_mask=atom_mask,
            )

            total_loss = total_loss + smooth_lddt_loss

        # calculate loss breakdown

        loss_breakdown = DiffusionLossBreakdown(mse_loss, bond_loss, smooth_lddt_loss)

        return ElucidatedAtomDiffusionReturn(total_loss, denoised_atom_pos, loss_breakdown, sigmas)


# TODO: modules


class SmoothLDDTLoss(Module):
    """Algorithm 27."""

    @typecheck
    def __init__(self, nucleic_acid_cutoff: float = 30.0, other_cutoff: float = 15.0):
        super().__init__()
        self.nucleic_acid_cutoff = nucleic_acid_cutoff
        self.other_cutoff = other_cutoff

    @typecheck
    def forward(
        self,
        pred_coords: Float["b n 3"],  # type: ignore
        true_coords: Float["b n 3"],  # type: ignore
        is_dna: Bool["b n"],  # type: ignore
        is_rna: Bool["b n"],  # type: ignore
        coords_mask: Bool["b n"] | None = None,  # type: ignore
    ) -> Float[""]:  # type: ignore
        """
        Compute the SmoothLDDT loss.

        :param pred_coords: Predicted coordinates.
        :param true_coords: True coordinates.
        :param is_dna: A boolean Tensor denoting DNA atoms.
        :param is_rna: A boolean Tensor denoting RNA atoms.
        :param coords_mask: The coordinates mask.
        :return: The output tensor.
        """
        # Compute distances between all pairs of atoms
        pred_dists = torch.cdist(pred_coords, pred_coords)
        true_dists = torch.cdist(true_coords, true_coords)

        # Compute distance difference for all pairs of atoms
        dist_diff = torch.abs(true_dists - pred_dists)

        # Compute epsilon values
        eps = (
            F.sigmoid(0.5 - dist_diff)
            + F.sigmoid(1.0 - dist_diff)
            + F.sigmoid(2.0 - dist_diff)
            + F.sigmoid(4.0 - dist_diff)
        ) / 4.0

        # Restrict to bespoke inclusion radius
        is_nucleotide = is_dna | is_rna
        is_nucleotide_pair = einx.logical_and("b i, b j -> b i j", is_nucleotide, is_nucleotide)

        inclusion_radius = torch.where(
            is_nucleotide_pair,
            true_dists < self.nucleic_acid_cutoff,
            true_dists < self.other_cutoff,
        )

        # Compute mean, avoiding self term
        mask = inclusion_radius & ~torch.eye(
            pred_coords.shape[1], dtype=torch.bool, device=pred_coords.device
        )

        # Take into account variable lengthed atoms in batch
        if exists(coords_mask):
            paired_coords_mask = einx.logical_and("b i, b j -> b i j", coords_mask, coords_mask)
            mask = mask & paired_coords_mask

        # Calculate masked averaging
        lddt_sum = (eps * mask).sum(dim=(-1, -2))
        lddt_count = mask.sum(dim=(-1, -2))
        lddt = lddt_sum / lddt_count.clamp(min=1)

        return 1.0 - lddt.mean()


class WeightedRigidAlign(Module):
    """Algorithm 28."""

    @typecheck
    def forward(
        self,
        pred_coords: Float["b n 3"],  # type: ignore - predicted coordinates
        true_coords: Float["b n 3"],  # type: ignore - true coordinates
        weights: Float["b n"],  # type: ignore - weights for each atom
        mask: Bool["b n"] | None = None,  # type: ignore - mask for variable lengths
    ) -> Float["b n 3"]:  # type: ignore
        """
        Compute the weighted rigid alignment.

        The check for ambiguous rotation and low rank
        of cross-correlation between aligned point clouds
        is inspired by https://pytorch3d.readthedocs.io/en/latest/_modules/pytorch3d/ops/points_alignment.html.

        :param pred_coords: Predicted coordinates.
        :param true_coords: True coordinates.
        :param weights: Weights for each atom.
        :param mask: The mask for variable lengths.
        """
        batch_size, num_points, dim = pred_coords.shape

        if exists(mask):
            # zero out all predicted and true coordinates where not an atom
            pred_coords = einx.where("b n, b n c, -> b n c", mask, pred_coords, 0.0)
            true_coords = einx.where("b n, b n c, -> b n c", mask, true_coords, 0.0)
            weights = einx.where("b n, b n, -> b n", mask, weights, 0.0)

        # Take care of weights broadcasting for coordinate dimension
        weights = rearrange(weights, "b n -> b n 1")

        # Compute weighted centroids
        pred_centroid = (pred_coords * weights).sum(dim=1, keepdim=True) / weights.sum(
            dim=1, keepdim=True
        )
        true_centroid = (true_coords * weights).sum(dim=1, keepdim=True) / weights.sum(
            dim=1, keepdim=True
        )

        # Center the coordinates
        pred_coords_centered = pred_coords - pred_centroid
        true_coords_centered = true_coords - true_centroid

        if num_points < (dim + 1):
            logger.warning(
                "The size of one of the point clouds is <= dim+1. "
                + "`WeightedRigidAlign` cannot return a unique rotation."
            )

        # Compute the weighted covariance matrix
        cov_matrix = einsum(
            weights * true_coords_centered,
            pred_coords_centered,
            "b n i, b n j -> b i j",
        )

        # Compute the SVD of the covariance matrix
        U, S, V = torch.svd(cov_matrix)

        # Catch ambiguous rotation by checking the magnitude of singular values
        if (S.abs() <= 1e-15).any() and not (num_points < (dim + 1)):
            logger.warning(
                "Excessively low rank of "
                + "cross-correlation between aligned point clouds. "
                + "`WeightedRigidAlign` cannot return a unique rotation."
            )

        # Compute the rotation matrix
        rot_matrix = einsum(U, V, "b i j, b k j -> b i k")

        # Ensure proper rotation matrix with determinant 1
        F = torch.eye(dim, dtype=cov_matrix.dtype, device=cov_matrix.device)[None].repeat(
            batch_size, 1, 1
        )
        F[:, -1, -1] = torch.det(rot_matrix)
        rot_matrix = einsum(U, F, V, "b i j, b j k, b l k -> b i l")

        # Apply the rotation and translation
        aligned_coords = (
            einsum(pred_coords_centered, rot_matrix, "b n i, b j i -> b n j") + true_centroid
        )
        aligned_coords.detach_()

        return aligned_coords


class ExpressCoordinatesInFrame(Module):
    """Algorithm 29."""

    def __init__(self, eps=1e-8):
        super().__init__()
        self.eps = eps

    @typecheck
    def forward(
        self,
        coords: Float["b m 3"],  # type: ignore
        frame: Float["b m 3 3"] | Float["b 3 3"] | Float["3 3"],  # type: ignore
    ) -> Float["b m 3"]:  # type: ignore
        """
        Express coordinates in the given frame.

        :param coords: Coordinates to be expressed in the given frame.
        :param frame: Frames defined by three points.
        :return: The transformed coordinates.
        """

        if frame.ndim == 2:
            frame = rearrange(frame, "fr fc -> 1 1 fr fc")
        elif frame.ndim == 3:
            frame = rearrange(frame, "b fr fc -> b 1 fr fc")

        # Extract frame atoms
        a, b, c = frame.unbind(dim=-1)
        w1 = F.normalize(a - b, dim=-1, eps=self.eps)
        w2 = F.normalize(c - b, dim=-1, eps=self.eps)

        # Build orthonormal basis
        e1 = F.normalize(w1 + w2, dim=-1, eps=self.eps)
        e2 = F.normalize(w2 - w1, dim=-1, eps=self.eps)
        e3 = torch.cross(e1, e2, dim=-1)

        # Project onto frame basis
        d = coords - b
        transformed_coords = torch.stack(
            [
                einsum(d, e1, "... i, ... i -> ..."),
                einsum(d, e2, "... i, ... i -> ..."),
                einsum(d, e3, "... i, ... i -> ..."),
            ],
            dim=-1,
        )

        return transformed_coords


class ComputeAlignmentError(Module):
    """Algorithm 30."""

    @typecheck
    def __init__(self, eps: float = 1e-8):
        super().__init__()
        self.eps = eps
        self.express_coordinates_in_frame = ExpressCoordinatesInFrame()

    @typecheck
    def forward(
        self,
        pred_coords: Float["b n 3"],  # type: ignore
        true_coords: Float["b n 3"],  # type: ignore
        pred_frames: Float["b n 3 3"],  # type: ignore
        true_frames: Float["b n 3 3"],  # type: ignore
    ) -> Float["b n"]:  # type: ignore
        """
        Compute the alignment errors.

        :param pred_coords: Predicted coordinates.
        :param true_coords: True coordinates.
        :param pred_frames: Predicted frames.
        :param true_frames: True frames.
        """
        # Express predicted coordinates in predicted frames
        pred_coords_transformed = self.express_coordinates_in_frame(pred_coords, pred_frames)

        # Express true coordinates in true frames
        true_coords_transformed = self.express_coordinates_in_frame(true_coords, true_frames)

        # Compute alignment errors
        alignment_errors = torch.sqrt(
            torch.sum(
                (pred_coords_transformed - true_coords_transformed) ** 2,
                dim=-1,
            )
            + self.eps
        )

        return alignment_errors


class CentreRandomAugmentation(Module):
    """Algorithm 19."""

    @typecheck
    def __init__(self, trans_scale: float = 1.0):
        super().__init__()
        self.trans_scale = trans_scale
        self.register_buffer("dummy", torch.tensor(0), persistent=False)

    @property
    def device(self):
        """
        Return the device of the module.

        :return: The device of the module.
        """
        return self.dummy.device

    @typecheck
    def forward(self, coords: Float["b n 3"]) -> Float["b n 3"]:  # type: ignore
        """
        Compute the augmented coordinates.

        :param coords: The coordinates to be augmented by a random rotation and translation.
        :return: The augmented coordinates.
        """
        batch_size = coords.shape[0]

        # Center the coordinates
        centered_coords = coords - coords.mean(dim=1, keepdim=True)

        # Generate random rotation matrix
        rotation_matrix = self._random_rotation_matrix(batch_size)

        # Generate random translation vector
        translation_vector = self._random_translation_vector(batch_size)
        translation_vector = rearrange(translation_vector, "b c -> b 1 c")

        # Apply rotation and translation
        augmented_coords = (
            einsum(centered_coords, rotation_matrix, "b n i, b j i -> b n j") + translation_vector
        )

        return augmented_coords

    @typecheck
    def _random_rotation_matrix(self, batch_size: int) -> Float["b 3 3"]:  # type: ignore
        """
        Generate a random rotation matrix.

        :param batch_size: The batch size.
        :return: The random rotation matrix.
        """
        # Generate random rotation angles
        angles = torch.rand((batch_size, 3), device=self.device) * 2 * torch.pi

        # Compute sine and cosine of angles
        sin_angles = torch.sin(angles)
        cos_angles = torch.cos(angles)

        # Construct rotation matrix
        eye = torch.eye(3, device=self.device)
        rotation_matrix = repeat(eye, "i j -> b i j", b=batch_size).clone()

        rotation_matrix[:, 0, 0] = cos_angles[:, 0] * cos_angles[:, 1]
        rotation_matrix[:, 0, 1] = (
            cos_angles[:, 0] * sin_angles[:, 1] * sin_angles[:, 2]
            - sin_angles[:, 0] * cos_angles[:, 2]
        )
        rotation_matrix[:, 0, 2] = (
            cos_angles[:, 0] * sin_angles[:, 1] * cos_angles[:, 2]
            + sin_angles[:, 0] * sin_angles[:, 2]
        )
        rotation_matrix[:, 1, 0] = sin_angles[:, 0] * cos_angles[:, 1]
        rotation_matrix[:, 1, 1] = (
            sin_angles[:, 0] * sin_angles[:, 1] * sin_angles[:, 2]
            + cos_angles[:, 0] * cos_angles[:, 2]
        )
        rotation_matrix[:, 1, 2] = (
            sin_angles[:, 0] * sin_angles[:, 1] * cos_angles[:, 2]
            - cos_angles[:, 0] * sin_angles[:, 2]
        )
        rotation_matrix[:, 2, 0] = -sin_angles[:, 1]
        rotation_matrix[:, 2, 1] = cos_angles[:, 1] * sin_angles[:, 2]
        rotation_matrix[:, 2, 2] = cos_angles[:, 1] * cos_angles[:, 2]

        return rotation_matrix

    @typecheck
    def _random_translation_vector(self, batch_size: int) -> Float["b 3"]:  # type: ignore
        """
        Generate a random translation vector.

        :param batch_size: The batch size.
        :return: The random translation vector.
        """
        # Generate random translation vector
        translation_vector = torch.randn((batch_size, 3), device=self.device) * self.trans_scale
        return translation_vector


# input embedder


class EmbeddedInputs(NamedTuple):
    """The EmbeddedInputs class."""

    single_inputs: Float["b n ds"]  # type: ignore
    single_init: Float["b n ds"]  # type: ignore
    pairwise_init: Float["b n n dp"]  # type: ignore
    atom_feats: Float["b m da"]  # type: ignore
    atompair_feats: Float["b m m dap"]  # type: ignore


class InputFeatureEmbedder(Module):
    """Algorithm 2."""

    def __init__(
        self,
        *,
        dim_atom_inputs,
        dim_atompair_inputs=5,
        atoms_per_window=27,
        dim_atom=128,
        dim_atompair=16,
        dim_token=384,
        dim_single=384,
        dim_pairwise=128,
        atom_transformer_blocks=3,
        atom_transformer_heads=4,
        atom_transformer_kwargs: dict = dict(),
    ):
        super().__init__()
        self.atoms_per_window = atoms_per_window

        self.to_atom_feats = LinearNoBias(dim_atom_inputs, dim_atom)

        self.to_atompair_feats = LinearNoBias(dim_atompair_inputs, dim_atompair)

        self.atom_repr_to_atompair_feat_cond = nn.Sequential(
            nn.LayerNorm(dim_atom),
            LinearNoBias(dim_atom, dim_atompair * 2),
            nn.ReLU(),
        )

        self.atompair_feats_mlp = nn.Sequential(
            LinearNoBias(dim_atompair, dim_atompair),
            nn.ReLU(),
            LinearNoBias(dim_atompair, dim_atompair),
            nn.ReLU(),
            LinearNoBias(dim_atompair, dim_atompair),
        )

        self.atom_transformer = DiffusionTransformer(
            depth=atom_transformer_blocks,
            heads=atom_transformer_heads,
            dim=dim_atom,
            dim_single_cond=dim_atom,
            dim_pairwise=dim_atompair,
            attn_window_size=atoms_per_window,
            **atom_transformer_kwargs,
        )

        self.atom_feats_to_pooled_token = AtomToTokenPooler(
            dim=dim_atom,
            dim_out=dim_token,
        )

        dim_single_input = dim_token + ADDITIONAL_RESIDUE_FEATS

        self.single_input_to_single_init = LinearNoBias(dim_single_input, dim_single)
        self.single_input_to_pairwise_init = LinearNoBiasThenOuterSum(
            dim_single_input, dim_pairwise
        )

    @typecheck
    def forward(
        self,
        *,
        atom_inputs: Float["b m dai"],  # type: ignore
        atompair_inputs: Float["b m m dapi"] | Float["b nw w1 w2 dapi"],  # type: ignore
        atom_mask: Bool["b m"],  # type: ignore
        additional_residue_feats: Float[f"b n {ADDITIONAL_RESIDUE_FEATS}"],  # type: ignore
        residue_atom_lens: Int["b n"],  # type: ignore
    ) -> EmbeddedInputs:
        """
        Compute the embedded inputs.

        :param atom_inputs: The atom inputs tensor.
        :param atompair_inputs: The atom pair inputs tensor.
        :param atom_mask: The atom mask tensor.
        :param additional_residue_feats: The additional residue features tensor.
        :param residue_atom_lens: The residue atom lengths tensor.
        :return: The embedded inputs.
        """
        assert (
            additional_residue_feats.shape[-1] == ADDITIONAL_RESIDUE_FEATS
        ), "Additional residue features must have 10 dimensions."

        w = self.atoms_per_window

        atom_feats = self.to_atom_feats(atom_inputs)
        atompair_feats = self.to_atompair_feats(atompair_inputs)

        # window the atom pair features before passing to atom encoder and decoder

        is_windowed = atompair_inputs.ndim == 5

        if not is_windowed:
            atompair_feats = full_pairwise_repr_to_windowed(atompair_feats, window_size=w)

        # condition atompair with atom repr

        atom_feats_cond = self.atom_repr_to_atompair_feat_cond(atom_feats)

        atom_feats_cond = pad_and_window(atom_feats_cond, w)

        atom_feats_cond_row, atom_feats_cond_col = atom_feats_cond.chunk(2, dim=-1)
        atom_feats_cond_col = concat_neighboring_windows(
            atom_feats_cond_col, dim_seq=1, dim_window=-2
        )

        atompair_feats = einx.add(
            "b nw w1 w2 dap, b nw w1 dap", atompair_feats, atom_feats_cond_row
        )
        atompair_feats = einx.add(
            "b nw w1 w2 dap, b nw w2 dap", atompair_feats, atom_feats_cond_col
        )

        # initial atom transformer

        atom_feats = self.atom_transformer(
            atom_feats, single_repr=atom_feats, pairwise_repr=atompair_feats
        )

        atompair_feats = self.atompair_feats_mlp(atompair_feats) + atompair_feats

        single_inputs = self.atom_feats_to_pooled_token(
            atom_feats=atom_feats,
            atom_mask=atom_mask,
            residue_atom_lens=residue_atom_lens,
        )

        single_inputs = torch.cat((single_inputs, additional_residue_feats), dim=-1)

        single_init = self.single_input_to_single_init(single_inputs)
        pairwise_init = self.single_input_to_pairwise_init(single_inputs)

        return EmbeddedInputs(
            single_inputs,
            single_init,
            pairwise_init,
            atom_feats,
            atompair_feats,
        )


# distogram head


class DistogramHead(Module):
    """A module for the distogram head."""

    @typecheck
    def __init__(
        self,
        *,
        dim_pairwise=128,
        num_dist_bins=38,  # think it is 38?
    ):
        super().__init__()

        self.to_distogram_logits = nn.Sequential(
            LinearNoBias(dim_pairwise, num_dist_bins),
            Rearrange("b ... l -> b l ..."),
        )

    @typecheck
    def forward(
        self, pairwise_repr: Float["b n n d"]  # type: ignore
    ) -> Float["b l n n"]:  # type: ignore
        """
        Perform the forward pass.

        :param pairwise_repr: The pairwise representation tensor.
        :return: The logits for the distogram.
        """
        logits = self.to_distogram_logits(pairwise_repr)
        return logits


# confidence head


class ConfidenceHeadLogits(NamedTuple):
    """The ConfidenceHeadLogits class."""

    pae: Float["b pae n n"] | None  # type: ignore
    pde: Float["b pde n n"]  # type: ignore
    plddt: Float["b plddt n"]  # type: ignore
    resolved: Float["b 2 n"]  # type: ignore


class ConfidenceHead(Module):
    """Algorithm 31."""

    @typecheck
    def __init__(self, *, dim_single_inputs, atompair_dist_bins: Float[" d"], dim_single=384, dim_pairwise=128, num_plddt_bins=50, num_pde_bins=64, num_pae_bins=64, pairformer_depth=4, pairformer_kwargs: dict = dict()):  # type: ignore
        super().__init__()

        self.register_buffer("atompair_dist_bins", atompair_dist_bins)

        num_dist_bins = atompair_dist_bins.shape[-1]
        self.num_dist_bins = num_dist_bins

        self.dist_bin_pairwise_embed = nn.Embedding(num_dist_bins, dim_pairwise)
        self.single_inputs_to_pairwise = LinearNoBiasThenOuterSum(dim_single_inputs, dim_pairwise)

        # pairformer stack

        self.pairformer_stack = PairformerStack(
            dim_single=dim_single,
            dim_pairwise=dim_pairwise,
            depth=pairformer_depth,
            **pairformer_kwargs,
        )

        # to predictions

        self.to_pae_logits = nn.Sequential(
            LinearNoBias(dim_pairwise, num_pae_bins),
            Rearrange("b ... l -> b l ..."),
        )

        self.to_pde_logits = nn.Sequential(
            LinearNoBias(dim_pairwise, num_pde_bins),
            Rearrange("b ... l -> b l ..."),
        )

        self.to_plddt_logits = nn.Sequential(
            LinearNoBias(dim_single, num_plddt_bins),
            Rearrange("b ... l -> b l ..."),
        )

        self.to_resolved_logits = nn.Sequential(
            LinearNoBias(dim_single, 2), Rearrange("b ... l -> b l ...")
        )

    @typecheck
    def forward(
        self,
        *,
        single_inputs_repr: Float["b n dsi"],  # type: ignore
        single_repr: Float["b n ds"],  # type: ignore
        pairwise_repr: Float["b n n dp"],  # type: ignore
        pred_atom_pos: Float["b n 3"],  # type: ignore
        mask: Bool["b n"] | None = None,  # type: ignore
        return_pae_logits=True,
    ) -> ConfidenceHeadLogits:
        """
        Compute the confidence head logits.

        :param single_inputs_repr: The single inputs representation tensor.
        :param single_repr: The single representation tensor.
        :param pairwise_repr: The pairwise representation tensor.
        :param pred_atom_pos: The predicted atom positions tensor.
        :param mask: The mask tensor.
        :param return_pae_logits: Whether to return the predicted aligned error (PAE) logits.
        :return: The confidence head logits.
        """

        pairwise_repr = pairwise_repr + self.single_inputs_to_pairwise(single_inputs_repr)

        # interatomic distances - embed and add to pairwise

        interatom_dist = torch.cdist(pred_atom_pos, pred_atom_pos, p=2)

        dist_from_dist_bins = einx.subtract(
            "b m dist, dist_bins -> b m dist dist_bins",
            interatom_dist,
            self.atompair_dist_bins,
        ).abs()
        dist_bin_indices = dist_from_dist_bins.argmin(dim=-1)
        pairwise_repr = pairwise_repr + self.dist_bin_pairwise_embed(dist_bin_indices)

        # pairformer stack

        single_repr, pairwise_repr = self.pairformer_stack(
            single_repr=single_repr, pairwise_repr=pairwise_repr, mask=mask
        )

        # to logits

        symmetric_pairwise_repr = pairwise_repr + rearrange(pairwise_repr, "b i j d -> b j i d")
        pde_logits = self.to_pde_logits(symmetric_pairwise_repr)

        plddt_logits = self.to_plddt_logits(single_repr)
        resolved_logits = self.to_resolved_logits(single_repr)

        # they only incorporate pae at some stage of training

        pae_logits = None

        if return_pae_logits:
            pae_logits = self.to_pae_logits(pairwise_repr)

        # return all logits

        return ConfidenceHeadLogits(pae_logits, pde_logits, plddt_logits, resolved_logits)


# main class


class LossBreakdown(NamedTuple):
    """The LossBreakdown class."""

    total_loss: Float[""]  # type: ignore
    total_diffusion: Float[""]  # type: ignore
    distogram: Float[""]  # type: ignore
    pae: Float[""]  # type: ignore
    pde: Float[""]  # type: ignore
    plddt: Float[""]  # type: ignore
    resolved: Float[""]  # type: ignore
    confidence: Float[""]  # type: ignore
    diffusion_mse: Float[""]  # type: ignore
    diffusion_bond: Float[""]  # type: ignore
    diffusion_smooth_lddt: Float[""]  # type: ignore


class AlphaFold3(Module):
    """Algorithm 1."""

    @typecheck
    def __init__(
        self,
        *,
        dim_atom_inputs,
        dim_template_feats,
        dim_template_model=64,
        atoms_per_window=27,
        dim_atom=128,
        dim_atompair_inputs=5,
        dim_atompair=16,
        dim_input_embedder_token=384,
        dim_single=384,
        dim_pairwise=128,
        dim_token=768,
        distance_bins: Float[" dist_bins"] = torch.linspace(3, 20, 38),  # type: ignore
        ignore_index=-1,
        num_dist_bins: int | None = None,
        num_plddt_bins=50,
        num_pde_bins=64,
        num_pae_bins=64,
        sigma_data=16,
        diffusion_num_augmentations=4,
        loss_confidence_weight=1e-4,
        loss_distogram_weight=1e-2,
        loss_diffusion_weight=4.0,
        input_embedder_kwargs: dict = dict(
            atom_transformer_blocks=3,
            atom_transformer_heads=4,
            atom_transformer_kwargs=dict(),
        ),
        confidence_head_kwargs: dict = dict(pairformer_depth=4),
        template_embedder_kwargs: dict = dict(
            pairformer_stack_depth=2,
            pairwise_block_kwargs=dict(),
        ),
        msa_module_kwargs: dict = dict(
            depth=4,
            dim_msa=64,
            dim_msa_input=None,
            outer_product_mean_dim_hidden=32,
            msa_pwa_dropout_row_prob=0.15,
            msa_pwa_heads=8,
            msa_pwa_dim_head=32,
            pairwise_block_kwargs=dict(),
        ),
        pairformer_stack: dict = dict(
            depth=48,
            pair_bias_attn_dim_head=64,
            pair_bias_attn_heads=16,
            dropout_row_prob=0.25,
            pairwise_block_kwargs=dict(),
        ),
        relative_position_encoding_kwargs: dict = dict(
            r_max=32,
            s_max=2,
        ),
        diffusion_module_kwargs: dict = dict(
            single_cond_kwargs=dict(
                num_transitions=2,
                transition_expansion_factor=2,
            ),
            pairwise_cond_kwargs=dict(num_transitions=2),
            atom_encoder_depth=3,
            atom_encoder_heads=4,
            token_transformer_depth=24,
            token_transformer_heads=16,
            atom_decoder_depth=3,
            atom_decoder_heads=4,
            serial=True,  # believe they have an error on Algorithm 23. lacking a residual - default to serial architecture until further news
        ),
        edm_kwargs: dict = dict(
            sigma_min=0.002,
            sigma_max=80,
            rho=7,
            P_mean=-1.2,
            P_std=1.2,
            S_churn=80,
            S_tmin=0.05,
            S_tmax=50,
            S_noise=1.003,
        ),
        augment_kwargs: dict = dict(),
    ):
        super().__init__()

        # atoms per window

        self.atoms_per_window = atoms_per_window

        # augmentation

        self.num_augmentations = diffusion_num_augmentations
        self.augmenter = CentreRandomAugmentation(**augment_kwargs)

        # input feature embedder

        self.input_embedder = InputFeatureEmbedder(
            dim_atom_inputs=dim_atom_inputs,
            dim_atompair_inputs=dim_atompair_inputs,
            atoms_per_window=atoms_per_window,
            dim_atom=dim_atom,
            dim_atompair=dim_atompair,
            dim_token=dim_input_embedder_token,
            dim_single=dim_single,
            dim_pairwise=dim_pairwise,
            **input_embedder_kwargs,
        )

        dim_single_inputs = dim_input_embedder_token + ADDITIONAL_RESIDUE_FEATS

        # relative positional encoding
        # used by pairwise in main alphafold2 trunk
        # and also in the diffusion module separately from alphafold3

        self.relative_position_encoding = RelativePositionEncoding(
            dim_out=dim_pairwise, **relative_position_encoding_kwargs
        )

        # token bonds
        # Algorithm 1 - line 5

        self.token_bond_to_pairwise_feat = nn.Sequential(
            Rearrange("... -> ... 1"), LinearNoBias(1, dim_pairwise)
        )

        # templates

        self.template_embedder = TemplateEmbedder(
            dim_template_feats=dim_template_feats,
            dim=dim_template_model,
            dim_pairwise=dim_pairwise,
            **template_embedder_kwargs,
        )

        # msa

        self.msa_module = MSAModule(
            dim_single=dim_single,
            dim_pairwise=dim_pairwise,
            **msa_module_kwargs,
        )

        # main pairformer trunk, 48 layers

        self.pairformer = PairformerStack(
            dim_single=dim_single,
            dim_pairwise=dim_pairwise,
            **pairformer_stack,
        )

        # recycling related

        self.recycle_single = nn.Sequential(
            nn.LayerNorm(dim_single), LinearNoBias(dim_single, dim_single)
        )

        self.recycle_pairwise = nn.Sequential(
            nn.LayerNorm(dim_pairwise),
            LinearNoBias(dim_pairwise, dim_pairwise),
        )

        # diffusion

        self.diffusion_module = DiffusionModule(
            dim_pairwise_trunk=dim_pairwise,
            dim_pairwise_rel_pos_feats=dim_pairwise,
            atoms_per_window=atoms_per_window,
            dim_pairwise=dim_pairwise,
            sigma_data=sigma_data,
            dim_atom=dim_atom,
            dim_atompair=dim_atompair,
            dim_token=dim_token,
            dim_single=dim_single + dim_single_inputs,
            **diffusion_module_kwargs,
        )

        self.edm = ElucidatedAtomDiffusion(
            self.diffusion_module, sigma_data=sigma_data, **edm_kwargs
        )

        # logit heads

        self.register_buffer("distance_bins", distance_bins)
        num_dist_bins = default(num_dist_bins, len(distance_bins))

        assert (
            len(distance_bins) == num_dist_bins
        ), "The argument `distance_bins` must have a length equal to the `num_dist_bins` passed in."

        self.distogram_head = DistogramHead(dim_pairwise=dim_pairwise, num_dist_bins=num_dist_bins)

        self.confidence_head = ConfidenceHead(
            dim_single_inputs=dim_single_inputs,
            atompair_dist_bins=distance_bins,
            dim_single=dim_single,
            dim_pairwise=dim_pairwise,
            num_plddt_bins=num_plddt_bins,
            num_pde_bins=num_pde_bins,
            num_pae_bins=num_pae_bins,
            **confidence_head_kwargs,
        )

        # loss related

        self.ignore_index = ignore_index
        self.loss_distogram_weight = loss_distogram_weight
        self.loss_confidence_weight = loss_confidence_weight
        self.loss_diffusion_weight = loss_diffusion_weight

        self.register_buffer("zero", torch.tensor(0.0), persistent=False)

    @property
    def device(self):
        """
        Return the device of the module.

        :return: The device of the module.
        """
        return self.zero.device

    @typecheck
    def forward(
        self,
        *,
        atom_inputs: Float["b m dai"],  # type: ignore
        atompair_inputs: Float["b m m dapi"] | Float["b nw w1 w2 dapi"],  # type: ignore
        additional_residue_feats: Float[f"b n {ADDITIONAL_RESIDUE_FEATS}"],  # type: ignore
        residue_atom_lens: Int["b n"],  # type: ignore
        atom_mask: Bool["b m"] | None = None,  # type: ignore
        token_bond: Bool["b n n"] | None = None,  # type: ignore
        msa: Float["b s n d"] | None = None,  # type: ignore
        msa_mask: Bool["b s"] | None = None,  # type: ignore
        templates: Float["b t n n dt"] | None = None,  # type: ignore
        template_mask: Bool["b t"] | None = None,  # type: ignore
        num_recycling_steps: int = 1,
        diffusion_add_bond_loss: bool = False,
        diffusion_add_smooth_lddt_loss: bool = False,
        residue_atom_indices: Int["b n"] | None = None,  # type: ignore
        num_sample_steps: int | None = None,
        atom_pos: Float["b m 3"] | None = None,  # type: ignore
        distance_labels: Int["b n n"] | None = None,  # type: ignore
        pae_labels: Int["b n n"] | None = None,  # type: ignore
        pde_labels: Int["b n n"] | None = None,  # type: ignore
        plddt_labels: Int["b n"] | None = None,  # type: ignore
        resolved_labels: Int["b n"] | None = None,  # type: ignore
        return_loss_breakdown=False,
        return_loss_if_possible: bool = True,
        num_rollout_steps: int = 20,
    ) -> Float["b m 3"] | Float[""] | Tuple[Float[""], LossBreakdown]:  # type: ignore
        """
        Run the forward pass of AlphaFold 3.

        :param atom_inputs: The atom inputs tensor.
        :param atompair_inputs: The atom pair inputs tensor.
        :param additional_residue_feats: The additional residue features tensor.
        :param residue_atom_lens: The residue atom lengths tensor.
        :param atom_mask: The atom mask tensor.
        :param token_bond: The token bond tensor.
        :param msa: The multiple sequence alignment tensor.
        :param msa_mask: The multiple sequence alignment mask tensor.
        :param templates: The templates tensor.
        :param template_mask: The template mask tensor.
        :param num_recycling_steps: The number of recycling steps.
        :param diffusion_add_bond_loss: Whether to add a bond loss in the diffusion module.
        :param diffusion_add_smooth_lddt_loss: Whether to add a smooth LDDT loss in the diffusion module.
        :param residue_atom_indices: The residue atom indices tensor.
        :param num_sample_steps: The number of sample steps.
        :param atom_pos: The atom positions tensor.
        :param distance_labels: The distance labels tensor.
        :param pae_labels: The predicted aligned error (PAE) labels tensor.
        :param pde_labels: The predicted distance error (PDE) labels tensor.
        :param plddt_labels: The predicted lDDT labels tensor.
        :param resolved_labels: The resolved labels tensor.
        :param return_loss_breakdown: Whether to return the loss breakdown.
        :param return_loss_if_possible: Whether to return the loss if possible.
        :return: The atomic coordinates or the loss.
        """
        atom_seq_len = atom_inputs.shape[-2]

        assert exists(residue_atom_lens) or exists(
            atom_mask
        ), "Either `residue_atom_lens` or `atom_mask` must be provided."

        # if atompair inputs are not windowed, window it

        is_atompair_inputs_windowed = atompair_inputs.ndim == 5

        if not is_atompair_inputs_windowed:
            atompair_inputs = full_pairwise_repr_to_windowed(
                atompair_inputs, window_size=self.atoms_per_window
            )

        # handle atom mask

        total_atoms = residue_atom_lens.sum(dim=-1)
        atom_mask = lens_to_mask(total_atoms, max_len=atom_seq_len)

        # handle offsets for residue atom indices

        if exists(residue_atom_indices):
            residue_atom_indices += F.pad(residue_atom_lens, (-1, 1), value=0)

        # get atom sequence length and residue sequence length depending on whether using packed atomic seq

        seq_len = residue_atom_lens.shape[-1]

        # embed inputs

        (
            single_inputs,
            single_init,
            pairwise_init,
            atom_feats,
            atompair_feats,
        ) = self.input_embedder(
            atom_inputs=atom_inputs,
            atompair_inputs=atompair_inputs,
            atom_mask=atom_mask,
            additional_residue_feats=additional_residue_feats,
            residue_atom_lens=residue_atom_lens,
        )

        # relative positional encoding

        relative_position_encoding = self.relative_position_encoding(
            additional_residue_feats=additional_residue_feats
        )

        pairwise_init = pairwise_init + relative_position_encoding

        # token bond features

        if exists(token_bond):
            # well do some precautionary standardization
            # (1) mask out diagonal - token to itself does not count as a bond
            # (2) symmetrize, in case it is not already symmetrical (could also throw an error)

            token_bond = token_bond | rearrange(token_bond, "b i j -> b j i")
            diagonal = torch.eye(seq_len, device=self.device, dtype=torch.bool)
            token_bond.masked_fill_(diagonal, False)
        else:
            seq_arange = torch.arange(seq_len, device=self.device)
            token_bond = einx.subtract("i, j -> i j", seq_arange, seq_arange).abs() == 1

        token_bond_feats = self.token_bond_to_pairwise_feat(token_bond.float())

        pairwise_init = pairwise_init + token_bond_feats

        # residue mask and pairwise mask

        total_atoms = residue_atom_lens.sum(dim=-1)
        mask = lens_to_mask(total_atoms, max_len=seq_len)

        pairwise_mask = einx.logical_and("b i, b j -> b i j", mask, mask)

        # init recycled single and pairwise

        recycled_pairwise = recycled_single = None
        single = pairwise = None

        # for each recycling step

        for _ in range(num_recycling_steps):
            # handle recycled single and pairwise if not first step

            recycled_single = recycled_pairwise = 0.0

            if exists(single):
                recycled_single = self.recycle_single(single)

            if exists(pairwise):
                recycled_pairwise = self.recycle_pairwise(pairwise)

            single = single_init + recycled_single
            pairwise = pairwise_init + recycled_pairwise

            # else go through main transformer trunk from alphafold2

            # templates

            if exists(templates):
                embedded_template = self.template_embedder(
                    templates=templates,
                    template_mask=template_mask,
                    pairwise_repr=pairwise,
                    mask=mask,
                )

                pairwise = embedded_template + pairwise

            # msa

            if exists(msa):
                embedded_msa = self.msa_module(
                    msa=msa,
                    single_repr=single,
                    pairwise_repr=pairwise,
                    mask=mask,
                    msa_mask=msa_mask,
                )

                pairwise = embedded_msa + pairwise

            # main attention trunk (pairformer)

            single, pairwise = self.pairformer(
                single_repr=single, pairwise_repr=pairwise, mask=mask
            )

        # determine whether to return loss if any labels were to be passed in
        # otherwise will sample the atomic coordinates

        atom_pos_given = exists(atom_pos)

        confidence_head_labels = (
            pae_labels,
            pde_labels,
            plddt_labels,
            resolved_labels,
        )
        all_labels = (distance_labels, *confidence_head_labels)

        has_labels = any([*map(exists, all_labels)])

        return_loss = atom_pos_given or has_labels

        # if neither atom positions or any labels are passed in, sample a structure and return

        if not return_loss_if_possible or not return_loss:
            return self.edm.sample(
                num_sample_steps=num_sample_steps,
                atom_feats=atom_feats,
                atompair_feats=atompair_feats,
                atom_mask=atom_mask,
                mask=mask,
                single_trunk_repr=single,
                single_inputs_repr=single_inputs,
                pairwise_trunk=pairwise,
                pairwise_rel_pos_feats=relative_position_encoding,
                residue_atom_lens=residue_atom_lens,
            )

        # losses default to 0

        distogram_loss = (
            diffusion_loss
        ) = confidence_loss = pae_loss = pde_loss = plddt_loss = resolved_loss = self.zero

        # calculate distogram logits and losses

        ignore = self.ignore_index

        # distogram head

        if not exists(distance_labels) and atom_pos_given and exists(residue_atom_indices):
            residue_pos = einx.get_at("b [m] c, b n -> b n c", atom_pos, residue_atom_indices)

            residue_dist = torch.cdist(residue_pos, residue_pos, p=2)
            dist_from_dist_bins = einx.subtract(
                "b m dist, dist_bins -> b m dist dist_bins",
                residue_dist,
                self.distance_bins,
            ).abs()
            distance_labels = dist_from_dist_bins.argmin(dim=-1)

        if exists(distance_labels):
            distance_labels = torch.where(pairwise_mask, distance_labels, ignore)
            distogram_logits = self.distogram_head(pairwise)
            distogram_loss = F.cross_entropy(
                distogram_logits, distance_labels, ignore_index=ignore
            )

        # otherwise, noise and make it learn to denoise

        calc_diffusion_loss = exists(atom_pos)

        if calc_diffusion_loss:
            num_augs = self.num_augmentations

            # take care of augmentation
            # they did 48 during training, as the trunk did the heavy lifting

            if num_augs > 1:
                (
                    atom_pos,
                    atom_mask,
                    atom_feats,
                    atompair_feats,
                    mask,
                    pairwise_mask,
                    single,
                    single_inputs,
                    pairwise,
                    relative_position_encoding,
                    additional_residue_feats,
                    residue_atom_indices,
                    residue_atom_lens,
                    pae_labels,
                    pde_labels,
                    plddt_labels,
                    resolved_labels,
                ) = tuple(
                    maybe(repeat)(t, "b ... -> (b a) ...", a=num_augs)
                    for t in (
                        atom_pos,
                        atom_mask,
                        atom_feats,
                        atompair_feats,
                        mask,
                        pairwise_mask,
                        single,
                        single_inputs,
                        pairwise,
                        relative_position_encoding,
                        additional_residue_feats,
                        residue_atom_indices,
                        residue_atom_lens,
                        pae_labels,
                        pde_labels,
                        plddt_labels,
                        resolved_labels,
                    )
                )

                atom_pos = self.augmenter(atom_pos)

            (
                diffusion_loss,
                denoised_atom_pos,
                diffusion_loss_breakdown,
                _,
            ) = self.edm(
                atom_pos,
                additional_residue_feats=additional_residue_feats,
                add_smooth_lddt_loss=diffusion_add_smooth_lddt_loss,
                add_bond_loss=diffusion_add_bond_loss,
                atom_feats=atom_feats,
                atompair_feats=atompair_feats,
                atom_mask=atom_mask,
                mask=mask,
                single_trunk_repr=single,
                single_inputs_repr=single_inputs,
                pairwise_trunk=pairwise,
                pairwise_rel_pos_feats=relative_position_encoding,
                residue_atom_lens=residue_atom_lens,
                return_denoised_pos=True,
            )

        # confidence head

        should_call_confidence_head = any([*map(exists, confidence_head_labels)])
        return_pae_logits = exists(pae_labels)

        if calc_diffusion_loss and should_call_confidence_head:
            # rollout

            pred_atom_pos = self.edm.sample(
                num_sample_steps=num_rollout_steps,
                atom_feats=atom_feats,
                atompair_feats=atompair_feats,
                atom_mask=atom_mask,
                mask=mask,
                single_trunk_repr=single,
                single_inputs_repr=single_inputs,
                pairwise_trunk=pairwise,
                pairwise_rel_pos_feats=relative_position_encoding,
                residue_atom_lens=residue_atom_lens,
            )

            pred_atom_pos = einx.get_at(
                "b [m] c, b n -> b n c", denoised_atom_pos, residue_atom_indices
            )

            logits = self.confidence_head(
                single_repr=single.detach(),
                single_inputs_repr=single_inputs.detach(),
                pairwise_repr=pairwise.detach(),
                pred_atom_pos=pred_atom_pos.detach(),
                mask=mask,
                return_pae_logits=return_pae_logits,
            )

            if exists(pae_labels):
                pae_labels = torch.where(pairwise_mask, pae_labels, ignore)
                pae_loss = F.cross_entropy(logits.pae, pae_labels, ignore_index=ignore)

            if exists(pde_labels):
                pde_labels = torch.where(pairwise_mask, pde_labels, ignore)
                pde_loss = F.cross_entropy(logits.pde, pde_labels, ignore_index=ignore)

            if exists(plddt_labels):
                plddt_labels = torch.where(mask, plddt_labels, ignore)
                plddt_loss = F.cross_entropy(logits.plddt, plddt_labels, ignore_index=ignore)

            if exists(resolved_labels):
                resolved_labels = torch.where(mask, resolved_labels, ignore)
                resolved_loss = F.cross_entropy(
                    logits.resolved, resolved_labels, ignore_index=ignore
                )

            confidence_loss = pae_loss + pde_loss + plddt_loss + resolved_loss

        # combine all the losses

        loss = (
            distogram_loss * self.loss_distogram_weight
            + diffusion_loss * self.loss_diffusion_weight
            + confidence_loss * self.loss_confidence_weight
        )

        if not return_loss_breakdown:
            return loss

        loss_breakdown = LossBreakdown(
            total_loss=loss,
            total_diffusion=diffusion_loss,
            pae=pae_loss,
            pde=pde_loss,
            plddt=plddt_loss,
            resolved=resolved_loss,
            distogram=distogram_loss,
            confidence=confidence_loss,
            **diffusion_loss_breakdown._asdict(),
        )

        return loss, loss_breakdown