"""Synthesize a Japanese sentence using the repository's real CPU TTS path."""
import hashlib
import importlib.util
import json
import os
import random
import sys
import time
from importlib.metadata import version
from pathlib import Path

project = Path.cwd()
sys.path.insert(0, str(project))
import numpy as np
import soundfile as sf
import torch
from config import settings
from local_tts_infer import TTSInferencer

text = "今日はいい天気ですね。"
random.seed(23)
np.random.seed(23)
torch.manual_seed(23)
torch.set_num_threads(2)
assert importlib.util.find_spec("jieba_fast") is None
assert version("jieba") == "0.42.1"
assert not torch.cuda.is_available()
reference = Path(settings.TTS_REF_AUDIO_JA).resolve()
assert reference.is_file()
before_load = time.perf_counter()
engine = TTSInferencer(device="cpu", gpt_path=settings.TTS_GPT_MODEL_PATH,
                       sovits_path=settings.TTS_SOVITS_MODEL_PATH)
before_speech = time.perf_counter()
sr, pcm = engine.infer(text=text, ref_audio_path=str(reference), prompt_text=settings.TTS_REF_TEXT_JA,
                       text_language="日文", prompt_language="日文")
wave = np.asarray(pcm, dtype=np.float32).reshape(-1)
assert sr > 0 and wave.size > sr // 4 and np.isfinite(wave).all(), (sr, wave.shape)
path = Path(sys.argv[1]) / "ja-first-sentence.wav"
sf.write(path, wave, sr, subtype="PCM_16")
decoded, rate = sf.read(path, dtype="float32")
rms = float(np.sqrt(np.mean(decoded.astype(np.float64)**2)))
assert rate == sr and decoded.size == wave.size and rms > 1e-4
result = {"text":text,"device":"cpu","model":str(engine.model_version),
          "torch":torch.__version__,"jieba":version("jieba"),"crcmod":version("crcmod"),
          "crcmod_native_extension":importlib.util.find_spec("crcmod._crcfunext") is not None,
          "sample_rate_hz":rate,"samples":decoded.size,"audio_seconds":round(decoded.size/rate,3),
          "rms":round(rms,6),"peak":round(float(np.abs(decoded).max()),6),
          "model_load_seconds":round(before_speech-before_load,2),
          "first_synthesis_seconds":round(time.perf_counter()-before_speech,2),
          "wav_sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
(path.parent/"synthesis.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("D1_SYNTHESIS "+json.dumps(result,ensure_ascii=True))
