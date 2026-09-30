"""The active Amadeus sample/logits_to_probs and infer_panel_naive semantics."""
from __future__ import annotations

import mlx.core as mx


def logits_to_probs(logits, previous_tokens=None, *, temperature=1.0,
                    top_k=None, top_p=None, repetition_penalty=1.0):
    # Torch modifies its caller's logits ONLY for repetition penalty. EOS
    # greedy argmax observes those values, not the later top-p/k transforms.
    # FP16 model output is promoted only at the sampler boundary, where
    # penalty, sorting, and exponential-race probabilities need the range.
    penalized = logits.astype(mx.float32) if logits.dtype == mx.float16 else logits
    if previous_tokens is not None and repetition_penalty != 1.0:
        score = mx.take_along_axis(penalized, previous_tokens, axis=1)
        score = mx.where(score < 0, score * repetition_penalty, score / repetition_penalty)
        penalized = mx.put_along_axis(penalized, previous_tokens, score, axis=1)
    filtered = penalized
    if top_p is not None and top_p < 1.0:
        indices = mx.argsort(-filtered, axis=-1)
        sorted_logits = mx.take_along_axis(filtered, indices, axis=-1)
        remove = mx.cumsum(mx.softmax(sorted_logits, axis=-1, precise=True), axis=-1) > top_p
        remove = remove & (mx.arange(filtered.shape[-1])[None, :] != 0)
        remove = mx.take_along_axis(remove, mx.argsort(indices, axis=-1), axis=-1)
        filtered = mx.where(remove, -float("inf"), filtered)
    filtered = filtered / max(temperature, 1e-5)
    if top_k is not None:
        if top_k <= 0:
            raise ValueError("top_k must be positive or None, matching the active Torch sampler")
        boundary = filtered.shape[-1] - min(top_k, filtered.shape[-1])
        pivot = mx.partition(filtered, boundary, axis=-1)[:, boundary:boundary + 1]
        filtered = mx.where(filtered < pivot, -float("inf"), filtered)
    return mx.softmax(filtered, axis=-1, precise=True), penalized


def sample(probs, *, noise=None):
    # Exponential race has the same target distribution as the Torch sampler.
    # RNG implementations differ; equal seeds are not a token-equivalence test.
    if noise is None:
        noise = -mx.log(mx.maximum(mx.random.uniform(shape=probs.shape), 1e-30))
    return mx.argmax(probs / noise, axis=-1, keepdims=True).astype(mx.int32)


def generate(model, phones, prompt, bert, *, top_k=5, top_p=1.0, temperature=1.0,
             repetition_penalty=1.35, early_stop_num=-1, max_steps=1500):
    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    history = prompt
    prefix_length = prompt.shape[1]
    logits, cache = model.prefill(phones, prompt, bert)
    try:
        for idx in range(max_steps):
            if idx < 11:
                logits = logits[:, :-1]
            probs, eos_logits = logits_to_probs(
                logits, history, top_k=top_k, top_p=top_p, temperature=temperature,
                repetition_penalty=repetition_penalty,
            )
            token = sample(probs)
            stop = (mx.argmax(eos_logits, axis=-1)[0] == model.config.eos) | (token[0, 0] == model.config.eos)
            history = mx.concatenate((history, token), axis=1)
            mx.async_eval(history, stop, cache.layers)
            # Only the forward is speculative. Sampling stays in this loop,
            # preserving RNG consumption even when EOS/budget stops generation.
            if idx + 1 < max_steps:
                logits, cache = model.decode_step(token, cache)
                mx.async_eval(logits, cache.layers)
            eos_stop = bool(stop.item())
            budget_stop = early_stop_num != -1 and history.shape[1] - prefix_length > early_stop_num
            if budget_stop or eos_stop:
                break
    finally:
        # Finish discarded lookahead before releasing the decoder's request
        # lock, including asynchronous errors. This is a request-end drain,
        # separate from the single scalar observation in each normal step.
        mx.eval(history, logits, cache.layers)
    # idx counts preceding generated tokens, including on budget/loop exit.
    # Preserve the existing last-token removal rather than correcting it here.
    return history[:, :-1], idx
