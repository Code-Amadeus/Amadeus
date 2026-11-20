"""
Cross-sentence speech-token prefix A/B test
===========================================
Version A: independent inference for each sentence, matching the current main flow.
Version B: appends PREFIX_LEN speech tokens from the previous sentence to prompts.

Design principles:
  - Do not modify any main-flow files.
  - Monkey-patch infer_panel to capture pred_semantic.
  - Temporarily replace the session-cache prompt and restore it immediately.
  - Reuse infer_stream for decoding instead of reimplementing v3 decoding.

Run from the project root:
    python tools/test_sentence_prefix/ab_test.py

Output: tools/test_sentence_prefix/output/
    A_01.wav ... A_full.wav   independent inference
    B_01.wav ... B_full.wav   cross-sentence continuation
"""

import os
import sys
import numpy as np
import torch
import soundfile as sf

# --- Paths ----------------------------------------------------------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(OUT_DIR, exist_ok=True)

# --- Configuration --------------------------------------------------------
from config.settings import (
    TTS_GPT_MODEL_PATH, TTS_SOVITS_MODEL_PATH,
    TTS_REF_AUDIO_JA, TTS_DEVICE,
)

gpt_path    = TTS_GPT_MODEL_PATH    if os.path.isabs(TTS_GPT_MODEL_PATH)    else os.path.join(ROOT, TTS_GPT_MODEL_PATH)
sovits_path = TTS_SOVITS_MODEL_PATH if os.path.isabs(TTS_SOVITS_MODEL_PATH) else os.path.join(ROOT, TTS_SOVITS_MODEL_PATH)
ref_audio   = TTS_REF_AUDIO_JA      if os.path.isabs(TTS_REF_AUDIO_JA)      else os.path.join(ROOT, TTS_REF_AUDIO_JA)
device      = TTS_DEVICE if (TTS_DEVICE == "cpu" or torch.cuda.is_available()) else "cpu"

# Number of speech tokens to reuse from the previous sentence.
PREFIX_LEN = 32

# Which span to take from the previous sentence's generated tokens.
# "tail"   : the last PREFIX_LEN tokens; can bias the model toward early EOS.
# "middle" : a middle span that avoids the EOS-adjacent tail; recommended first.
# "head"   : the first PREFIX_LEN tokens, representing sentence onset state.
PREFIX_SOURCE = "middle"   # "tail" | "middle" | "head"

PROMPT_TEXT = "そういえば,正式に自己紹介していませんでしたね……牧瀬紅莉栖です.改めてまして,よろしく。"
PROMPT_LANG = "日文"
TEXT_LANG   = "日文"
SAMPLE_RATE = 24000

# Test sentences: question -> answer -> explanation -> supplement -> impression.
# This sequence makes prosody carry-over easier to hear.
SENTENCES = [
    "ねえ、量子力学って本当に難しいと思う？",
    "難しいは難しいけど、理解できない訳じゃないわ。",
    "大事なのは、観測と確率の概念をちゃんと掴むことね。",
    "波動関数が何を意味するのかさえわかれば、あとは数学の問題よ。",
    "まあ、私も最初は混乱したけどね。",
]

# --- Model initialization -------------------------------------------------
print(f"[init] GPT : {gpt_path}")
print(f"[init] SoVITS: {sovits_path}")
from local_tts_infer import TTSInferencer
inferencer = TTSInferencer(device=device, gpt_path=gpt_path, sovits_path=sovits_path)
t2s = inferencer.t2s_model.model
print("[init] model ready.")

# --- Session cache: reference-audio features ------------------------------
lang_map      = inferencer.dict_language
prompt_lang_c = lang_map.get(PROMPT_LANG, "all_ja")

sess = inferencer._build_session_cache(ref_audio, PROMPT_TEXT, prompt_lang_c)
print(f"[init] base_prompt shape: {sess['prompt'].shape}")

# --- infer_panel capture hook ---------------------------------------------
# Patch t2s.infer_panel to capture the current sentence's generated speech
# tokens. Restore the original function immediately after capture.

_captured: list = []   # Stores (generated_tokens_1d,) after each infer_panel call.

_orig_infer_panel = t2s.infer_panel.__func__   # Original unbound method.

def _hooked_infer_panel(self_t2s, *args, **kwargs):
    pred_sem, idx = _orig_infer_panel(self_t2s, *args, **kwargs)
    # pred_sem[:, -idx:] is the current sentence's new tokens, excluding prompt.
    gen = pred_sem[:, -idx:][0].clone()   # shape: (idx,)
    _captured.append(gen)
    return pred_sem, idx

# --- Core inference helper ------------------------------------------------
silence_200ms = np.zeros(int(SAMPLE_RATE * 0.2), dtype=np.float32)

def infer_sentence(text: str, extra_prompt_suffix=None) -> tuple[np.ndarray, torch.Tensor]:
    """
    Run inference for one sentence and return (audio_np, generated_tokens_1d).

    extra_prompt_suffix: when provided, append it to sess["prompt"] temporarily
    and restore the original prompt immediately after the call.
    """
    global _captured

    # 1. Temporarily replace the session prompt for version B.
    original_prompt = sess["prompt"]
    if extra_prompt_suffix is not None:
        sess["prompt"] = torch.cat(
            [original_prompt, extra_prompt_suffix.unsqueeze(0)], dim=1
        )
        print(f"  prompt: {original_prompt.shape[1]} -> {sess['prompt'].shape[1]} tokens")

    # 2. Install the capture hook.
    _captured = []
    import types
    t2s.infer_panel = types.MethodType(_hooked_infer_panel, t2s)

    # 3. Run through infer_stream, including v3 decoding.
    chunks = []
    try:
        for sr, audio_chunk, _ in inferencer.infer_stream(
            text=text,
            ref_audio_path=ref_audio,
            prompt_text=PROMPT_TEXT,
            text_language=TEXT_LANG,
            prompt_language=PROMPT_LANG,
            how_to_cut="不切",        # Sentences are already split externally.
            top_k=20, top_p=0.6, temperature=0.6,
            enable_cuda_graph=False,  # Offline test; avoid CUDA graph bucket issues.
            enable_static_kv=False,
        ):
            if audio_chunk is not None and len(audio_chunk) > 0:
                chunks.append(audio_chunk)
    finally:
        # 4. Restore the session prompt and infer_panel.
        sess["prompt"] = original_prompt
        t2s.infer_panel = types.MethodType(_orig_infer_panel, t2s)

    audio_np = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    # 5. Use the last capture; infer_stream may call infer_panel multiple times.
    gen_tokens = _captured[-1] if _captured else torch.zeros(0, dtype=torch.long, device=device)

    return audio_np, gen_tokens


# --- Version A: independent inference -------------------------------------
print("\n" + "=" * 60)
print("Version A - independent inference (no cross-sentence prefix)")
print("=" * 60)

a_chunks = []
for i, sent in enumerate(SENTENCES):
    print(f"\n[A-{i+1:02d}] {sent}")
    audio, _ = infer_sentence(sent)
    path = os.path.join(OUT_DIR, f"A_{i+1:02d}.wav")
    sf.write(path, audio, SAMPLE_RATE)
    a_chunks += [audio, silence_200ms.copy()]
    print(f"  -> {path}  ({len(audio)/SAMPLE_RATE:.2f}s)")

sf.write(os.path.join(OUT_DIR, "A_full.wav"), np.concatenate(a_chunks), SAMPLE_RATE)
print("\n[A] -> A_full.wav")

# --- Version B: cross-sentence continuation -------------------------------
print("\n" + "=" * 60)
print(f"Version B - cross-sentence continuation (PREFIX_LEN={PREFIX_LEN})")
print("=" * 60)

b_chunks  = []
prev_suf  = None   # PREFIX_LEN speech tokens from the previous sentence.

for i, sent in enumerate(SENTENCES):
    print(f"\n[B-{i+1:02d}] {sent}")

    audio, gen_tokens = infer_sentence(sent, extra_prompt_suffix=prev_suf)

    # Select this sentence's tokens for the next sentence.
    n = gen_tokens.shape[0]
    if n < PREFIX_LEN:
        # Too few generated tokens; skip prefixing short sentences.
        prev_suf = None
    elif PREFIX_SOURCE == "middle":
        # Avoid the EOS-adjacent tail and take a middle span instead.
        # Leave max(PREFIX_LEN//2, 8) tail tokens untouched.
        tail_skip = max(PREFIX_LEN // 2, 8)
        end_idx   = max(n - tail_skip, PREFIX_LEN)  # Ensure enough room.
        start_idx = end_idx - PREFIX_LEN
        prev_suf  = gen_tokens[start_idx:end_idx].clone()
    elif PREFIX_SOURCE == "head":
        prev_suf = gen_tokens[:PREFIX_LEN].clone()
    else:  # "tail"
        prev_suf = gen_tokens[-PREFIX_LEN:].clone()

    path = os.path.join(OUT_DIR, f"B_{i+1:02d}.wav")
    sf.write(path, audio, SAMPLE_RATE)
    b_chunks += [audio, silence_200ms.copy()]
    tok_info = f"gen={n} -> prefix={'none' if prev_suf is None else f'{PREFIX_LEN} [{PREFIX_SOURCE}]'}"
    print(f"  -> {path}  ({len(audio)/SAMPLE_RATE:.2f}s)  {tok_info}")

sf.write(os.path.join(OUT_DIR, "B_full.wav"), np.concatenate(b_chunks), SAMPLE_RATE)
print("\n[B] -> B_full.wav")

# --- Done -----------------------------------------------------------------
print("\n" + "=" * 60)
print(f"Output directory: {OUT_DIR}")
print("")
print("Recommended comparison order:")
print("  1. A_full.wav vs B_full.wav        overall prosody")
print("  2. A_0N.wav   vs B_0N.wav          sentence starts and onset naturalness")
print("")
print("How to judge results:")
print("  If B is more natural: try PREFIX_LEN=64 or consider wiring it into the main flow")
print(f"  If B has odd sentence starts: halve PREFIX_LEN ({PREFIX_LEN//2}) and retry")
print("  If B produces abnormal lengths: the prefix strategy is out-of-distribution")
print("=" * 60)
