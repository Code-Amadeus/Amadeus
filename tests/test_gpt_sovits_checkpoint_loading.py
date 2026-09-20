from __future__ import annotations

from pathlib import Path

import pytest


torch = pytest.importorskip("torch", reason="test requires the local-model tier")

from GPT_SoVITS.process_ckpt import (
    HParams,
    get_sovits_version_from_path_fast,
    load_sovits_new,
)
from GPT_SoVITS import process_ckpt


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
        (b"00", ["v1", "v1", False]),
        (b"01", ["v2", "v2", False]),
        (b"02", ["v2", "v3", False]),
        (b"03", ["v2", "v3", True]),
        (b"05", ["v2", "v2Pro", False]),
        (b"06", ["v2", "v2ProPlus", False]),
    ],
)
def test_version_detector_recognizes_architecture_headers(
    tmp_path: Path,
    prefix: bytes,
    expected: list[object],
) -> None:
    standard = tmp_path / "standard.pth"
    data = _write_checkpoint(standard)
    prefixed = tmp_path / "prefixed.pth"
    prefixed.write_bytes(prefix + data[2:])

    assert get_sovits_version_from_path_fast(prefixed) == expected


@pytest.mark.parametrize(
    ("digest", "size", "model_version"),
    [
        ("c7e9fce2223f3db685cdfa1e6368728a", 162303657, "v2Pro"),
        ("66b313e39455b57ab1b0bc0b239c9d0a", 200125741, "v2ProPlus"),
    ],
)
def test_pretrained_v2pro_identity_overrides_legacy_size_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    digest: str,
    size: int,
    model_version: str,
) -> None:
    # Official base models have a PK header and sizes otherwise classified as
    # v2. Supply their verified 8192-byte prefix hashes without bundling weights
    # or requiring network access in CI. A renamed file must retain its identity.
    checkpoint = tmp_path / "renamed.pth"
    _write_checkpoint(checkpoint)
    monkeypatch.setattr(process_ckpt, "get_hash_from_file", lambda _path: digest)
    monkeypatch.setattr(process_ckpt.os.path, "getsize", lambda _path: size)

    assert get_sovits_version_from_path_fast(checkpoint) == ["v2", model_version, False]


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (82978 * 1024 - 1, ("v1", "v1", False)),
        (82978 * 1024, ("v2", "v2", False)),
        (700 * 1024 * 1024, ("v2", "v3", False)),
    ],
)
def test_unrecognized_pk_checkpoint_keeps_legacy_size_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
    expected: tuple[str, str, bool],
) -> None:
    checkpoint = tmp_path / "legacy.pth"
    _write_checkpoint(checkpoint)
    monkeypatch.setattr(process_ckpt.os.path, "getsize", lambda _path: size)

    assert get_sovits_version_from_path_fast(checkpoint) == expected
