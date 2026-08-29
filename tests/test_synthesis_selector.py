"""Synthesis backend selector tests.

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_synthesis_selector.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tts.synthesis_backend import SynthesisBackends, select_synthesis


async def _fake_backend(*args, **kwargs):
    return None


def _backends() -> SynthesisBackends:
    return SynthesisBackends(
        cuda_graph=_fake_backend,
        experimental=_fake_backend,
        default=_fake_backend,
    )


def test_cuda_graph_precedes_experimental():
    name, _ = select_synthesis(
        object(),
        cuda_graph_enabled=True,
        experimental_enabled=True,
        backends=_backends(),
    )
    assert name == "cuda_graph_serial"


def test_experimental_precedes_default():
    name, _ = select_synthesis(
        object(),
        cuda_graph_enabled=False,
        experimental_enabled=True,
        backends=_backends(),
    )
    assert name == "experimental_asyncio_queue"


def test_default_backend_when_flags_disabled():
    name, _ = select_synthesis(
        object(),
        cuda_graph_enabled=False,
        experimental_enabled=False,
        backends=_backends(),
    )
    assert name == "enhanced"


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all synthesis selector tests passed")


if __name__ == "__main__":
    _main()
