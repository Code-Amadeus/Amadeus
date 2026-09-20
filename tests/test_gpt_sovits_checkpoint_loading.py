from __future__ import annotations

from pathlib import Path

import pytest


torch = pytest.importorskip("torch", reason="test requires the local-model tier")

from GPT_SoVITS.process_ckpt import (
    HParams,
    get_sovits_version_from_path_fast,
    load_sovits_new,
)


def _write_checkpoint(path: Path) -> bytes:
    payload = {
        "weight": {"fixture": torch.tensor([1.0])},
        "config": HParams(data={"sampling_rate": 24000}),
    }
    torch.save(payload, path)
    return path.read_bytes()


def test_safe_loader_accepts_standard_checkpoint(tmp_path: Path) -> None:
    checkpoint = tmp_path / "standard.pth"
    _write_checkpoint(checkpoint)

    loaded = load_sovits_new(checkpoint)

    assert loaded["config"].data.sampling_rate == 24000
    assert loaded["weight"]["fixture"].item() == 1.0


def test_safe_loader_restores_gpt_sovits_version_prefix(tmp_path: Path) -> None:
    standard = tmp_path / "standard.pth"
    data = _write_checkpoint(standard)
    prefixed = tmp_path / "prefixed.pth"
    prefixed.write_bytes(b"02" + data[2:])

    loaded = load_sovits_new(prefixed)

    assert loaded["config"].data.sampling_rate == 24000


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        (b"05", ["v2", "v2Pro", False]),
        (b"06", ["v2", "v2ProPlus", False]),
    ],
)
def test_version_detector_recognizes_v2pro_headers(
    tmp_path: Path,
    prefix: bytes,
    expected: list[object],
) -> None:
    standard = tmp_path / "standard.pth"
    data = _write_checkpoint(standard)
    prefixed = tmp_path / "prefixed.pth"
    prefixed.write_bytes(prefix + data[2:])

    assert get_sovits_version_from_path_fast(prefixed) == expected
