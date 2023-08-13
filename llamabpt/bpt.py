import os
from shutil import copyfile
from typing import Any, Dict, List, Optional, Tuple, Union
import json
import tempfile
from typing import Callable, NamedTuple, Optional
from einops import rearrange
import functools

import numpy as np
import jax
import jax.numpy as jnp
from jax import lax
from tux import get_gradient_checkpoint_policy
from flax.linen import partitioning as nn_partitioning
import flax.linen as nn


def _chunk_attention_bias(query_chunk_size, key_chunk_size,
            bias, deterministic, attn_dropout, attn_pdrop, causal,
            dtype, query_chunk_idx, key_chunk_idx):
    query_offset = query_chunk_idx * query_chunk_size
    key_offset = key_chunk_idx * key_chunk_size
    chunk_bias = jnp.zeros((1, 1, 1, 1), dtype=dtype)
    if bias is not None:
        chunk_bias = lax.dynamic_slice(
            bias,
            start_indices=(0, 0, query_offset, key_offset),
            slice_sizes=(*bias.shape[:2], min(bias.shape[-2], query_chunk_size), min(bias.shape[-1], key_chunk_size)),
        )

    if causal:
        query_idx = lax.broadcasted_iota(dtype=jnp.int32, shape=(query_chunk_size, 1), dimension=0)
        key_idx = lax.broadcasted_iota(dtype=jnp.int32, shape=(1, key_chunk_size), dimension=1)
        offset = query_offset - key_offset
        query_idx += offset
        causal_mask_value = (query_idx < key_idx) * jnp.finfo(dtype).min
        chunk_bias += causal_mask_value.reshape(1, 1, *causal_mask_value.shape)

    if not deterministic and attn_pdrop > 0.0:
        attn_dropout_slice = lax.dynamic_slice(
            attn_dropout,
            start_indices=(0, 0, query_offset, key_offset),
            slice_sizes=(
                *attn_dropout.shape[:2],
                min(attn_dropout.shape[-2], query_chunk_size),
                min(attn_dropout.shape[-1], key_chunk_size),
            ),
        )
        chunk_bias += attn_dropout_slice * jnp.finfo(dtype).min
    return chunk_bias.astype(dtype)


class Carry(NamedTuple):
    numerator: jax.Array
    denominator: jax.Array
    max_so_far: jax.Array


def blockwise_attn(query, key, value, bias, deterministic,
        dropout_rng, attn_pdrop, causal, query_chunk_size,
        key_chunk_size, dtype, policy, precision, float32_logits):
    query = query / jnp.sqrt(query.shape[-1]).astype(dtype)
    q_len = query.shape[1]
    kv_len = key.shape[1]
    if float32_logits:
        query = query.astype(jnp.float32)
        key = key.astype(jnp.float32)
    query = rearrange(query, 'b (c n) h d -> n b c h d', c=query_chunk_size)
    key, value = map(lambda t: rearrange(t, 'b (c n) h d -> n b c h d', c=key_chunk_size), (key, value))
    num_q, batch, _, num_heads, dim_per_head = query.shape
    num_kv = key.shape[0]

    for bias_dim, broadcast_dim in zip(bias.shape, (batch, num_heads, q_len, kv_len)):
        assert bias_dim == 1 or bias_dim == broadcast_dim
    if not deterministic and attn_pdrop > 0.0:
        attn_dropout_rng, dropout_rng = jax.random.split(dropout_rng)
        attn_dropout = jax.random.bernoulli(attn_dropout_rng, attn_pdrop, (batch, num_heads, q_len, kv_len))
    else:
        attn_dropout = None

    _chunk_bias_fn = functools.partial(
        _chunk_attention_bias,
        query_chunk_size, key_chunk_size, bias, deterministic,
        attn_dropout, attn_pdrop, causal, dtype)

    def _query_chunk_attention(args):
        query_chunk, query_chunk_idx = args

        @functools.partial(jax.checkpoint, prevent_cse=False,
                           policy=get_gradient_checkpoint_policy(policy))
        def summarize_chunk(carry, args):
            key_chunk, value_chunk, key_chunk_idx = args
            (numerator, denominator, prev_max_score) = carry
            attn_weights = jnp.einsum('bqhd,bkhd->bqhk', query_chunk, key_chunk, precision=precision)
            bias_chunk = _chunk_bias_fn(query_chunk_idx, key_chunk_idx)
            bias_chunk = jnp.moveaxis(bias_chunk, 1, 2)
            attn_weights = attn_weights + bias_chunk

            max_score = jnp.max(attn_weights, axis=-1, keepdims=True)
            max_score = jnp.maximum(prev_max_score, max_score)
            max_score = jax.lax.stop_gradient(max_score)
            exp_weights = jnp.exp(attn_weights - max_score)
            exp_values = jnp.einsum(
                'bqhv,bvhf->bqhf', exp_weights, value_chunk, precision=precision
            )
            correction = jnp.exp(prev_max_score - max_score)
            numerator = numerator * correction + exp_values
            denominator = denominator * correction + exp_weights.sum(axis=-1, keepdims=True)
            return Carry(numerator, denominator, max_score), None

        def skip_upper_half(carry, args):
            key_chunk, value_chunk, key_chunk_idx = args
            skip_block = jnp.array(False)
            if causal:
                skip_block = query_chunk_idx < key_chunk_idx
            return jax.lax.cond(
                skip_block,
                lambda carry, args: (carry, None),
                summarize_chunk,
                carry,
                args,
            )

        init_carry = Carry(
            jnp.zeros((batch, query_chunk_size, num_heads, dim_per_head), dtype=query.dtype),
            jnp.zeros((batch, query_chunk_size, num_heads, dim_per_head), dtype=query.dtype),
            (-jnp.inf) * jnp.ones((batch, query_chunk_size, num_heads, 1), dtype=query.dtype),
        )
        (numerator, denominator, max_score), _ = lax.scan(
            skip_upper_half, init_carry, xs=(key, value, jnp.arange(0, num_kv))
        )
        outputs = (numerator / denominator).astype(dtype)
        return outputs

    _, res = lax.scan(
        lambda _, x: ((), _query_chunk_attention(x)),
        (), xs=(query, jnp.arange(0, num_q))
    )
    res = rearrange(res, 'n b c h d -> b (n c) h d')
    return res


def blockwise_ffn(cell, inputs, chunk_size, deterministic, policy):
    inputs = rearrange(inputs, 'b (c n) d -> b c n d', c=chunk_size)
    def ffn(cell, carry, hidden_states):
        outputs = cell.forward_ffn(hidden_states, deterministic=deterministic)
        return carry, outputs
    ffn_remat = nn_partitioning.remat(
        ffn,
        variables="params",
        prevent_cse=False,
        policy=get_gradient_checkpoint_policy(policy),
    )
    scan_axis = inputs.ndim - 2
    _, res = nn.scan(
        ffn_remat,
        variable_broadcast="params",
        split_rngs={"params": False, "dropout": True},
        in_axes=scan_axis,
        out_axes=scan_axis,
    )(cell, None, inputs)
    res = rearrange(res, 'b c n d -> b (c n) d')
    return res


def blockwise_cross_entropy(logits, tokens, chunk_size, policy, valid=None):
    if valid is None:
        valid = jnp.ones(tokens.shape[:2])
    valid = valid.astype(jnp.float32)
    logits = jnp.reshape(logits, (-1, logits.shape[-1]))
    tokens = jnp.reshape(tokens, (-1,))
    valid = jnp.reshape(valid, (-1,))

    def loss_acc(logits, tokens, valid):
        valid_text_length = jnp.maximum(jnp.sum(valid, axis=-1), 1e-10)

        token_log_prob = jnp.squeeze(
            jnp.take_along_axis(
                jax.nn.log_softmax(logits, axis=-1),
                jnp.expand_dims(tokens, -1),
                axis=-1,
            ),
            -1,
        )
        token_log_prob = jnp.where(valid > 0.0, token_log_prob, jnp.array(0.0))
        correct = jnp.where(
            valid > 0.0,
            jnp.argmax(logits, axis=-1) == tokens,
            jnp.array(False)
        )
        return token_log_prob, correct, valid_text_length
    @functools.partial(jax.checkpoint, prevent_cse=False,
             policy=get_gradient_checkpoint_policy(policy))
    def _loss_and_accuracy(carry, args):
        loss, accuracy, num = carry
        logits, tokens, valid = args
        token_log_prob, correct, valid_text_length = \
            loss_acc(logits, tokens, valid)
        loss = loss + jnp.sum(token_log_prob, axis=-1) / valid_text_length
        accuracy = accuracy + jnp.sum(correct, axis=-1) / valid_text_length
        num = num + 1
        return (loss, accuracy, num), None
    logits = rearrange(logits, '(n c) d -> n c d', c=chunk_size)
    tokens = rearrange(tokens, '(n c) -> n c', c=chunk_size)
    valid = rearrange(valid, '(n c) -> n c', c=chunk_size)
    (loss, accuracy, num), _ = jax.lax.scan(
        _loss_and_accuracy, (0.0, 0.0, 0), xs=(logits, tokens, valid)
    )
    loss = - loss / num
    accuracy = accuracy / num
    return loss, accuracy
