"""Offline signal replay through the shipped AEC and barge-in detector."""
import asyncio
import json
import os
import sys
import time
from importlib.metadata import version
from math import gcd
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.update(TTS_DEVICE="cpu", AEC_REALTIME_ENABLED="true", AEC_REALTIME_BARGE_IN="true")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from asr.barge_in_detector import BargeInDetector
from asr.mic_input_service import MicFrame
from tts.aec_realtime import RealtimeAECProcessor

RATE = 16000
STEP = 160
CHUNK = 512
TOTAL = 8 * RATE
NEAR_START = 4 * RATE
ECHO_LAG = 64 * RATE // 1000

def speech(path, length, peak):
    src, rate = sf.read(path, dtype="float32", always_2d=True)
    g = gcd(rate, RATE)
    data = resample_poly(src.mean(axis=1), RATE // g, rate // g)
    active = np.flatnonzero(abs(data) > 0.06 * abs(data).max())
    assert active.size, "recording is silent"
    data = data[max(0, active[0]-320):active[-1]+1]
    data = data / np.max(abs(data)) * peak
    return np.resize(data, length).astype(np.float32)

class ReplayMic:
    mic_index = 0
    def __init__(self, detector, aec, far, near):
        self.detector = detector
        self.aec = aec
        self.far = far
        self.raw = near.copy()
        self.raw[ECHO_LAG:] += 0.42 * far[:-ECHO_LAG]
        self.residual = np.zeros_like(self.raw)
        self.processed = self.read_pos = 0
        self.clock_origin = time.monotonic()
        self.handoffs = []

    def start(self):
        pass

    def cursor(self):
        return self

    def read(self, timeout=0.25):
        end = self.read_pos + CHUNK
        if end > self.raw.size:
            self.detector._stop_event.set()
            return None
        while self.processed < end:
            start = self.processed
            stop = min(start + STEP, self.raw.size)
            self.aec.push_reference(self.far[start:stop], RATE)
            self.residual[start:stop] = self.aec.process_mic(self.raw[start:stop], RATE)
            self.processed = stop
        start = self.read_pos
        self.read_pos = end
        sample = self.residual[start:end]
        return MicFrame(start // CHUNK, self.clock_origin + start / RATE,
                        sample, float(np.sqrt(np.mean(sample**2))), self.raw[start:end])

    def mark_handoff(self, seq, *, preroll_ms):
        self.handoffs.append(seq * CHUNK / RATE)

async def case(far, near):
    callbacks, debug = [], []
    async def interrupted():
        callbacks.append(True)
    detector = BargeInDetector(tts_playing_fn=lambda: True, on_barge_in=interrupted,
                               on_debug=lambda event: debug.append(event.get("status")))
    state, detail = await asyncio.to_thread(detector._probe_vad_capability)
    assert state == "ready", (state, detail)
    detector._loop = asyncio.get_running_loop()
    aec = RealtimeAECProcessor()
    assert aec.enabled and aec._ensure(), aec._init_error
    aec.set_delay_ms(64, reason="fixture echo delay")
    mic = ReplayMic(detector, aec, far, near)
    synthetic_clock = SimpleNamespace(monotonic=lambda: mic.clock_origin + mic.processed / RATE, time=time.time)
    with patch("asr.mic_input_service.get_mic_input_service", return_value=mic), \
         patch("asr.echo_guard.get_realtime_aec_processor", return_value=aec), \
         patch("tts.aec_realtime.time", synthetic_clock):
        await asyncio.wait_for(asyncio.to_thread(detector._run), timeout=50)
        await asyncio.sleep(0.05)
    power = lambda x: float(np.mean(np.square(x, dtype=np.float64)))
    window = slice(2*RATE, min(NEAR_START, mic.read_pos))
    in_power, out_power = power(mic.raw[window]), power(mic.residual[window])
    assert np.isfinite(in_power) and in_power > 1e-8 and np.isfinite(out_power), (in_power,out_power)
    return {"callback_count":len(callbacks), "handoff_speech_positions_s":mic.handoffs,
            "echo_attenuation_db_seconds_2_to_4":round(10*np.log10(in_power/max(out_power,1e-12)),2),
            "audio_replayed_s":mic.read_pos/RATE, "native_engine":type(aec._ap).__name__,
            "debug_events":debug}

async def main(far_file, near_file, report):
    far = speech(far_file, TOTAL, .50)
    near = np.zeros(TOTAL, dtype=np.float32)
    echo = await case(far, near)
    near[NEAR_START:] = speech(near_file, TOTAL-NEAR_START, .70)
    overlap = await case(far, near)
    result = {"platform":sys.platform, "python":sys.version.split()[0],
              "versions":{p:version(p) for p in ("aec-audio-processing","PyAudio","silero-vad")},
              "far_only":echo, "near_speech_during_playback":overlap, "near_start_s":NEAR_START/RATE,
              "fixture":"recorded near/far speech plus synthetic attenuated 64 ms echo, no audio devices"}
    print(json.dumps(result, indent=2))
    report.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    assert echo["callback_count"]==0 and not echo["handoff_speech_positions_s"],echo
    assert echo["echo_attenuation_db_seconds_2_to_4"] > 3,echo
    assert overlap["callback_count"] == 1 and len(overlap["handoff_speech_positions_s"])==1,overlap
    assert overlap["handoff_speech_positions_s"][0] >= NEAR_START/RATE - CHUNK/RATE,overlap

if __name__ == "__main__":
    asyncio.run(main(*map(Path,sys.argv[1:])))
