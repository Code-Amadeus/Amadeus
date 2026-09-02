"""L2 tier contract: barge-in must not start a doomed detector thread.

On an L2 install (silero-vad/torch absent) with barge-in config enabled, the
startup boundary must keep the detector off with an observable degradation
instead of spawning a thread that fails on import and emits barge_in_error on
every sentence. The capability fact is owned by BargeInDetector (it loads its
own VAD model) and must NOT be inferred from the ASRManager lifecycle: the
manager is created lazily when ASR first listens and cleared on idle unload,
while the detector only needs its own VAD — so keyboard-chat-then-first-
playback and post-unload journeys must still pass the gate.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# L2 = audio stack present, vad tier (silero-vad + its torch dependency) absent.
_L3_ONLY_MODULES = ("silero_vad", "torch", "torchaudio")

_PROBE = f"""
import sys
import types

_BLOCKED = {set(_L3_ONLY_MODULES)!r}

class _Blocker:
    # Simulate a missing package for `import` statements: the finder raises
    # ModuleNotFoundError, which is what a real L2 install raises.
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ModuleNotFoundError(
                f"{{name}} is not part of the L2 install", name=name
            )
        return None

sys.meta_path.insert(0, _Blocker())
for mod in list(sys.modules):
    if mod.split(".")[0] in _BLOCKED:
        del sys.modules[mod]

# Prove the mask is real: the vad tier is genuinely absent in this process.
try:
    import silero_vad  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    raise SystemExit("silero_vad unexpectedly importable under the L2 mask")

import server.app
from asr.barge_in_detector import BargeInDetector


async def _noop() -> None:
    return None


def _detector():
    return BargeInDetector(tts_playing_fn=lambda: False, on_barge_in=_noop)


# (a) L2 mask: silero_vad absent -> fallback, thread never started, and the
# verdict is cached (later playback sentences read the cache, no re-failure).
det = _detector()
assert det.vad_status() == ("fallback", ""), det.vad_status()
start, detail = server.app._barge_in_start_decision(det, config_enabled=True)
assert start is False and "fallback" in detail, (start, detail)
assert not det.running
start, detail = server.app._barge_in_start_decision(det, config_enabled=True)
assert start is False and "fallback" in detail, (start, detail)
assert not det.running

# (b) Degraded: silero_vad importable but model load fails. A broken install
# must stay observable with its reason, and probe exactly once.
broken = types.ModuleType("silero_vad")
_load_calls = 0

def _boom():
    global _load_calls
    _load_calls += 1
    raise RuntimeError("simulated model load failure")

broken.load_silero_vad = _boom
sys.modules["silero_vad"] = broken

det_broken = _detector()
state, reason = det_broken.vad_status()
assert state == "degraded" and "RuntimeError" in reason and "simulated" in reason, (state, reason)
start, detail = server.app._barge_in_start_decision(det_broken, config_enabled=True)
assert start is False and "degraded" in detail and "simulated" in detail, (start, detail)
assert not det_broken.running
assert _load_calls == 1, _load_calls
start, detail = server.app._barge_in_start_decision(det_broken, config_enabled=True)
assert start is False and "degraded" in detail, (start, detail)
assert _load_calls == 1, _load_calls

# (c) L3/L4 journey regression (the reviewed scenario): voice capability is
# ready (stubbed here — this CI process has no real deps) while asr_manager
# is still None (bootstrap not run, or idle-unloaded). The gate must pass.
assert server.app.asr_manager is None, "precondition: manager not created"

ready = types.ModuleType("silero_vad")
_ready_load_calls = 0

class _FakeModel:
    def eval(self):
        return self

def _fake_load():
    global _ready_load_calls
    _ready_load_calls += 1
    return _FakeModel()

ready.load_silero_vad = _fake_load
sys.modules["silero_vad"] = ready

det_ready = _detector()
assert det_ready.vad_status() == ("ready", ""), det_ready.vad_status()
start, detail = server.app._barge_in_start_decision(det_ready, config_enabled=True)
assert start is True and detail == "", (start, detail)
assert server.app.asr_manager is None  # the manager plays no role in the gate

# (d) Config switch off -> off, and no capability probe/log detail at all.
# The counter already holds (c)'s single probe; this decision must add none.
start, detail = server.app._barge_in_start_decision(_detector(), config_enabled=False)
assert start is False and detail == "", (start, detail)
assert _ready_load_calls == 1, _ready_load_calls

print("L2_BARGE_IN_GATE_OK")
"""


def test_barge_in_gate_disables_without_vad_tier() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"L2 barge-in gate probe failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    )
    assert "L2_BARGE_IN_GATE_OK" in result.stdout
