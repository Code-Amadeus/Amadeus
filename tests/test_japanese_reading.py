"""Optional context reading must preserve native identity and work offline."""
from types import SimpleNamespace
import hashlib
import json
from pathlib import Path

import pytest

from config import japanese_reading as pack
from tts import japanese_reading as runtime
from tools.validate_japanese_reading import target_reading


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch):
    monkeypatch.setattr(runtime, "_runtime_status", {"state": "not_loaded", "ready": False,
                                                   "identity": None, "version": "", "detail": "Not loaded"})


def _prepare(root, monkeypatch):
    directory = root / "assets" / pack.PACK_TREE
    directory.mkdir(parents=True)
    hashes = {}
    for name in pack.MODEL_HASHES:
        data = name.encode()
        (directory / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(pack, "MODEL_HASHES", hashes)
    (directory / "provenance.json").write_text(json.dumps({"version": pack.VERSION,
        "source": {"repository": pack.SOURCE_REPOSITORY, "revision": pack.SOURCE_REVISION, "directory": "v5"},
        "sha256": hashes}), encoding="utf-8")
    (directory / "NOTICE.md").write_text("Source and license", encoding="utf-8")
    (directory / "MIT.txt").write_text("MIT terms", encoding="utf-8")
    return directory


def _native():
    calls = []
    native = SimpleNamespace()
    native.tsqyomi = SimpleNamespace(unload_model=lambda: calls.append("unload"),
        load_model=lambda **kwargs: calls.append(kwargs), is_model_loaded=lambda: True)
    native.run_frontend = lambda text, **kwargs: calls.append((text, kwargs)) or []
    return native, calls


def test_optional_missing_and_disabled_models_keep_original_frontend(tmp_path):
    native, calls = _native()
    assert runtime.configure_japanese_reading(native, project_root=tmp_path, enabled=True)["state"] == "not_installed"
    runtime.run_frontend(native, "生きる")
    assert calls == [("生きる", {})]
    assert runtime.configure_japanese_reading(native, project_root=tmp_path, enabled=False)["state"] == "disabled"


def test_ready_model_uses_only_pinned_local_files_and_cpu(tmp_path, monkeypatch):
    directory = _prepare(tmp_path, monkeypatch)
    native, calls = _native()
    status = runtime.configure_japanese_reading(native, project_root=tmp_path, enabled=True)
    assert status["ready"] and status["identity"]
    assert calls == ["unload", {"model_dir": directory.resolve(), "onnx_providers": ["CPUExecutionProvider"],
                                "allow_provider_fallback": False}]
    runtime.run_frontend(native, "舞台の上手")
    assert calls[-1] == ("舞台の上手", {"use_tsqyomi": True})
    runtime.configure_japanese_reading(native, project_root=tmp_path, enabled=False)
    runtime.run_frontend(native, "舞台の上手")
    assert calls[-1] == ("舞台の上手", {})


@pytest.mark.parametrize("problem", ["corrupt", "missing", "native_failure", "unsupported"])
def test_invalid_optional_model_is_reported_without_breaking_base_frontend(tmp_path, monkeypatch, problem):
    directory = _prepare(tmp_path, monkeypatch)
    native, calls = _native()
    if problem == "corrupt":
        (directory / "model.onnx").write_bytes(b"wrong")
    elif problem == "missing":
        (directory / "tokenizer.json").unlink()
    elif problem == "native_failure":
        def fail(**_):
            raise RuntimeError("invalid ONNX contract")
        native.tsqyomi.load_model = fail
    else:
        del native.tsqyomi
    status = runtime.configure_japanese_reading(native, project_root=tmp_path, enabled=True)
    assert status["state"] == "invalid" and not status["ready"] and status["identity"] is None
    runtime.run_frontend(native, "人気")
    assert calls[-1] == ("人気", {})


def test_context_model_changes_actual_audio_cache_identity(monkeypatch):
    from tts import japanese_dictionary as dictionary
    monkeypatch.setattr(dictionary, "_runtime_status", {"state": "ready", "identity": "native-dictionary"})
    before = dictionary.dictionary_runtime_status()["identity"]
    monkeypatch.setattr(runtime, "_runtime_status", {"state": "ready", "ready": True, "identity": "model-v5"})
    after = dictionary.dictionary_runtime_status()["identity"]
    assert before != after and after == dictionary.dictionary_runtime_status()["identity"]
    monkeypatch.setattr(dictionary, "_runtime_status", {"identity": None})
    assert dictionary.dictionary_runtime_status()["identity"] is None


def test_golden_target_extraction_handles_native_merged_and_split_nodes():
    assert target_reading([{"string": "何と", "read": "ナント"}], "何と") == "ナント"
    assert target_reading([{"string": "日本", "read": "ニホン"}, {"string": "中", "read": "ジュウ"}], "日本中") == "ニホンジュウ"
    assert target_reading([{"string": "何と", "read": "ナント"}], "何") is None


def test_optional_model_pack_roundtrip_and_native_pin_boundary(tmp_path, monkeypatch):
    from tools import prepare_japanese_reading as preparation
    from tools.external_assets import build_bundle, install_bundle, verify_bundle

    source = tmp_path / "source"
    original = _prepare(source, monkeypatch)
    monkeypatch.setattr(preparation, "MODEL_HASHES", pack.MODEL_HASHES)
    producer = tmp_path / "producer"
    prepared = preparation.prepare(original, project_root=producer)
    index = Path(__file__).resolve().parents[1] / "assets/index.json"
    bundle = tmp_path / "context.zip"
    manifest = build_bundle(project_root=producer, output=bundle, pack_ids=[pack.PACK_ID], index_path=index)
    assert {item["path"] for item in manifest["files"]} == {f"assets/{pack.PACK_TREE}/{name}" for name in pack.PACK_FILES}
    assert verify_bundle(bundle, index_path=index) == manifest
    recipient = tmp_path / "日本語 with spaces"
    result = install_bundle(bundle, project_root=recipient, index_path=index)
    assert result["installed_files"] == len(pack.PACK_FILES)
    installed = recipient / "assets" / pack.PACK_TREE
    assert pack.load_reading_pack(installed).identity == pack.load_reading_pack(prepared).identity

    # An internally consistent ZIP is not authority to activate different weights.
    # Transport integrity belongs to the installer; native compatibility pins here.
    (prepared / "model.onnx").write_bytes(b"different model")
    replacement = tmp_path / "different.zip"
    build_bundle(project_root=producer, output=replacement, pack_ids=[pack.PACK_ID], index_path=index)
    install_bundle(replacement, project_root=recipient, index_path=index, overwrite=True)
    native, _ = _native()
    assert runtime.configure_japanese_reading(native, project_root=recipient, enabled=True)["state"] == "invalid"


def test_preparation_rejects_wrong_source_before_writing(tmp_path, monkeypatch):
    from tools import prepare_japanese_reading as preparation

    source = _prepare(tmp_path / "source", monkeypatch)
    monkeypatch.setattr(preparation, "MODEL_HASHES", pack.MODEL_HASHES)
    (source / "metadata.json").write_bytes(b"wrong metadata")
    destination = tmp_path / "destination"
    with pytest.raises(pack.JapaneseReadingError, match="Source model differs"):
        preparation.prepare(source, project_root=destination)
    assert not destination.exists()
