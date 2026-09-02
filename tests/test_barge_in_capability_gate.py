"""L2 tier contract: barge-in must not start a doomed detector thread.

On an L2 install (silero-vad/torch absent) with barge-in config enabled, the
startup boundary must keep the detector off with an observable degradation
instead of spawning a thread that fails on import and emits barge_in_error on
every sentence. The gate consumes the ASR manager's public vad_status().
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
from asr.manager import ASRManager


def _l2_manager():
    # Skip __init__ (backend/mic construction); the vad capability probe is
    # what the gate consumes and it runs synchronously inside _init_vad().
    mgr = ASRManager.__new__(ASRManager)
    mgr._init_vad()
    return mgr


# Real L2 capability chain: masked imports -> manager reports fallback.
mgr = _l2_manager()
assert mgr.vad_status() == ("fallback", ""), mgr.vad_status()

# Gate: config on + fallback tier -> stay off, state observable.
start, detail = server.app._barge_in_start_decision(mgr, config_enabled=True)
assert start is False, (start, detail)
assert "fallback" in detail, detail

# Degraded (installed but broken) must stay observable, not fake absence.
mgr._vad_degraded = "boom"
assert mgr.vad_status() == ("degraded", "boom"), mgr.vad_status()
start, detail = server.app._barge_in_start_decision(mgr, config_enabled=True)
assert start is False and "degraded" in detail and "boom" in detail, (start, detail)

# No observed capability (manager not created yet) -> stay off.
start, detail = server.app._barge_in_start_decision(None, config_enabled=True)
assert start is False and detail, (start, detail)


class _ReadyManager:
    def vad_status(self):
        return "ready", ""


# Ready tier passes the gate (normal-path contract; stub: no torch here).
start, detail = server.app._barge_in_start_decision(_ReadyManager(), config_enabled=True)
assert start is True and detail == "", (start, detail)

# Config switch off -> off, and callers must not log a capability detail.
start, detail = server.app._barge_in_start_decision(_ReadyManager(), config_enabled=False)
assert start is False and detail == "", (start, detail)

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
