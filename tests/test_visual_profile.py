from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from render.server import AssetServer
from render.visual_profile import VisualProfileStore, draft_profile, inspect_model
from server.character_presentation import CharacterPresentationCoordinator
from server.handlers.visual_handler import VisualHandler


def model_fixture(root: Path, expressions=("Smile", "Thinking", "Angry", "Disappointed")) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "model.moc3").write_bytes(b"synthetic-host-inspection-only")
    (root / "texture.png").write_bytes(b"synthetic-host-inspection-only")
    registered = []
    for name in expressions:
        filename = name + ".exp3.json"
        (root / filename).write_text(json.dumps({"Parameters": []}), encoding="utf-8")
        registered.append({"Name": name, "File": filename})
    settings = {
        "Version": 3,
        "FileReferences": {"Moc": "model.moc3", "Textures": ["texture.png"],
                           "Expressions": registered},
        "Groups": [{"Target": "Parameter", "Name": "LipSync", "Ids": ["ModelMouth"]}],
    }
    entry = root / "test.model3.json"
    entry.write_text(json.dumps(settings), encoding="utf-8")
    return entry


def configured_store(tmp_path):
    entry = model_fixture(tmp_path / "model")
    profile, _ = draft_profile(str(entry))
    core = tmp_path / "sdk/live2dcubismcore.min.js"
    core.parent.mkdir()
    core.write_text("// synthetic Host SDK selection test", encoding="utf-8")
    store = VisualProfileStore(tmp_path / "profiles.json", project_root=tmp_path)
    config = {"backend": "live2d", "selected_profile_id": profile["profile_id"],
              "core_path": str(core), "profiles": [profile]}
    store.save(config)
    return store, profile, entry, core


def test_profiles_are_visual_assets_persisted_without_changing_original_files(tmp_path):
    store, profile, entry, _ = configured_store(tmp_path)
    before = {p.name: p.read_bytes() for p in entry.parent.iterdir()}
    edited = store.config
    edited["profiles"][0]["emotion_map"]["work"] = "Thinking"
    edited["profiles"][0]["layouts"]["wallpaper"]["scale"] = 1.4
    store.save(edited)
    restored = VisualProfileStore(store.path, project_root=tmp_path)
    assert restored.config == edited
    assert restored.selected_profile()["profile_id"] == profile["profile_id"]
    assert restored.selected_profile()["kind"] == "live2d"
    assert before == {p.name: p.read_bytes() for p in entry.parent.iterdir()}
    forbidden = copy.deepcopy(edited)
    forbidden["profiles"][0]["character_id"] = "conversation-identity"
    with pytest.raises(ValueError, match="unsupported profile"):
        store.save(forbidden)


def test_default_mapping_uses_registered_semantic_name_before_alias(tmp_path):
    entry = model_fixture(tmp_path / "model", ("Sad", "Happy", "Normal"))
    profile, capabilities = draft_profile(str(entry))
    assert profile["emotion_map"]["sad"] == "Sad"
    assert profile["emotion_map"]["happy"] == "Happy"
    assert profile["emotion_map"]["normal"] == "Normal"
    assert profile["emotion_map"]["thinking"] is None
    assert capabilities["expressions"] == ["Sad", "Happy", "Normal"]


def test_broken_dormant_profile_does_not_lock_out_sprite_or_a_good_model(tmp_path):
    store, profile, entry, _ = configured_store(tmp_path)
    another = model_fixture(tmp_path / "other")
    other_profile, _ = draft_profile(str(another))
    config = store.config
    config["profiles"].append(other_profile)
    entry.write_text("[]", encoding="utf-8")
    config["backend"] = "sprite"
    store.save(config)
    assert store.runtime_config("render")["backend"] == "sprite"
    config["backend"] = "live2d"
    config["selected_profile_id"] = other_profile["profile_id"]
    store.save(config)
    assert "error" not in store.runtime_config("wallpaper")


def test_corrupt_saved_profile_file_is_preserved_until_explicit_repair(tmp_path):
    target = tmp_path / "profiles.json"
    original = "{ this file has user profiles but is damaged"
    target.write_text(original, encoding="utf-8")
    store = VisualProfileStore(target, project_root=tmp_path)
    assert store.load_error
    with pytest.raises(ValueError, match="original file is preserved"):
        store.save(store.config)
    assert target.read_text(encoding="utf-8") == original
    assert store.backend == "sprite"


@pytest.mark.parametrize("change,message", [
    (lambda value: [], "JSON object"),
    (lambda value: {**value, "Groups": [None]}, "group entries"),
    (lambda value: {**value, "Groups": {}}, "Groups"),
    (lambda value: {**value, "FileReferences": {**value["FileReferences"], "Motions": []}}, "Motions"),
    (lambda value: {**value, "FileReferences": {**value["FileReferences"], "Motions": {"Idle": [None]}}}, "motion entries"),
])
def test_invalid_model_shapes_report_a_specific_error_and_allow_sprite(tmp_path, change, message):
    store, _, entry, _ = configured_store(tmp_path)
    value = json.loads(entry.read_text(encoding="utf-8"))
    entry.write_text(json.dumps(change(value)), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        inspect_model(entry)
    runtime = store.runtime_config("render")
    assert message in runtime["error"]
    config = store.config
    config["backend"] = "sprite"
    store.save(config)
    assert "error" not in store.runtime_config("render")


def test_optional_invalid_physics_and_motion_shapes_are_observable_warnings(tmp_path):
    entry = model_fixture(tmp_path / "model")
    settings = json.loads(entry.read_text(encoding="utf-8"))
    settings["FileReferences"]["Physics"] = "bad.physics3.json"
    settings["FileReferences"]["Motions"] = {"Idle": [{"File": "bad.motion3.json"}]}
    (entry.parent / "bad.physics3.json").write_text("[]", encoding="utf-8")
    (entry.parent / "bad.motion3.json").write_text('{"Meta": [], "Curves": []}', encoding="utf-8")
    entry.write_text(json.dumps(settings), encoding="utf-8")
    capabilities, _ = inspect_model(entry)
    assert len(capabilities["warnings"]) == 2
    assert capabilities["expressions"]


def test_model_references_cannot_serve_files_outside_selected_package(tmp_path):
    entry = model_fixture(tmp_path / "model")
    (tmp_path / "outside.moc3").write_bytes(b"private")
    settings = json.loads(entry.read_text(encoding="utf-8"))
    settings["FileReferences"]["Moc"] = "../outside.moc3"
    entry.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(ValueError, match="leaves the selected model package"):
        inspect_model(entry)


def test_wallpaper_asset_mount_contains_only_declared_files_and_core_identity_changes(tmp_path):
    store, profile, entry, core = configured_store(tmp_path)
    (entry.parent / "private.txt").write_text("not an asset reference", encoding="utf-8")
    server = AssetServer(tmp_path, start_port=18340)
    port = server.start()
    try:
        first = store.runtime_config("wallpaper", asset_server=server)
        assert json.loads(urlopen(f"http://127.0.0.1:{port}" + first["model_url"]).read())["Version"] == 3
        with pytest.raises(HTTPError) as failure:
            urlopen(f"http://127.0.0.1:{port}/visual-model/{profile['profile_id']}/private.txt")
        assert failure.value.code == 404
        alternative = tmp_path / "other-sdk/live2dcubismcore.min.js"
        alternative.parent.mkdir()
        alternative.write_text("// another SDK", encoding="utf-8")
        edited = store.config
        edited["core_path"] = str(alternative)
        store.save(edited)
        second = store.runtime_config("wallpaper", asset_server=server)
        assert first["core_url"] != second["core_url"]
        with pytest.raises(HTTPError):
            urlopen(f"http://127.0.0.1:{port}" + first["core_url"])
    finally:
        server.stop()


def test_selection_serializes_resolving_and_writing_a_claim(tmp_path):
    store, _, _, _ = configured_store(tmp_path)
    config = store.config
    config["backend"] = "sprite"
    store.save(config)
    captured = []
    presentation = CharacterPresentationCoordinator(lambda *_: None, emit_now=lambda *args: captured.append(args))
    started = threading.Event()
    proceed = threading.Event()

    def slow_resolver(label, **metadata):
        payload = store.intent_payload(label, **metadata)
        started.set()
        assert proceed.wait(3)
        return payload

    presentation.set_payload_resolver(slow_resolver)
    claim_thread = threading.Thread(target=lambda: presentation.claim_now(
        source_kind="chat", source_id="sentence", label="thinking"))
    claim_thread.start()
    assert started.wait(3)
    selection_done = threading.Event()

    def change_selection():
        with presentation.selection_lock:
            edited = store.config
            edited["backend"] = "live2d"
            store.save(edited)
            presentation.set_payload_resolver(store.intent_payload)
            presentation.reproject()
        selection_done.set()

    selection_thread = threading.Thread(target=change_selection)
    selection_thread.start()
    assert not selection_done.wait(.05)
    proceed.set()
    claim_thread.join(3)
    selection_thread.join(3)
    assert not claim_thread.is_alive() and not selection_thread.is_alive()
    transition = presentation.current_transition()
    assert transition.payload["backend"] == "live2d"
    assert transition.payload["label"] == "thinking"
    assert transition.payload["semantic_label"] == "thinking"
    assert transition.owner.source_id == "sentence"


def test_source_recovery_uses_current_semantics_across_backend_changes(tmp_path):
    store, _, _, _ = configured_store(tmp_path)
    presentation = CharacterPresentationCoordinator(lambda *_: None, emit_now=lambda *_: None)
    presentation.set_payload_resolver(store.intent_payload)
    presentation.claim_now(source_kind="work", source_id="work", label="work", tier="ambient")
    presentation.claim_now(source_kind="chat", source_id="line", label="smile")
    config = store.config
    config["backend"] = "sprite"
    with presentation.selection_lock:
        store.save(config)
        presentation.reproject()
    assert presentation.current_transition().payload["label"] == "smile"
    presentation.release_now(source_kind="chat", source_id="line")
    restored = presentation.current_transition()
    assert restored.payload["label"] in {"speaking_trans", "thinking_trans"}
    assert restored.payload["semantic_label"] == "work"
    assert restored.owner.source_kind == "work"


def test_layout_save_does_not_reselect_sprite_random_route(tmp_path, monkeypatch):
    store, _, _, _ = configured_store(tmp_path)
    edited = store.config
    edited["backend"] = "sprite"
    store.save(edited)
    calls = []
    monkeypatch.setattr("render.spriteforge_intent.random.choice", lambda routes: calls.append(routes) or routes[0])
    presentation = CharacterPresentationCoordinator(lambda *_: None, emit_now=lambda *_: None)
    presentation.set_payload_resolver(store.intent_payload)
    presentation.claim_now(source_kind="work", source_id="work", label="work")
    route = presentation.current_transition().payload["label"]
    edited["profiles"][0]["layouts"]["wallpaper"]["scale"] = 1.5
    store.save(edited)
    assert presentation.current_transition().payload["label"] == route
    assert len(calls) == 1


def test_old_surface_receipts_cannot_reappear_after_new_selection(tmp_path, monkeypatch):
    store, profile, _, _ = configured_store(tmp_path)
    from server import character_presentation
    presentation = CharacterPresentationCoordinator(lambda *_: None, emit_now=lambda *_: None)
    monkeypatch.setattr("server.handlers.visual_handler.coordinator", presentation)
    monkeypatch.setattr("server.handlers.visual_handler.bus.emit_now", lambda *_: None)
    handler = VisualHandler()
    handler.configure(store, lambda: None)
    receipt = {"surface": "wallpaper", "profile_id": profile["profile_id"],
               "runtime_id": store.runtime_id, "revision": store.revision, "state": "ready"}
    assert handler.report_status(receipt)
    edited = store.config
    edited["backend"] = "sprite"
    asyncio.run(handler.handle("visual.save", {"config": edited}))
    assert not handler.report_status(receipt)
    assert handler.snapshot()["surfaces"] == {}
    assert not handler.report_status({**receipt, "runtime_id": "old-host", "revision": store.revision})
    current = {**receipt, "revision": store.revision}
    assert handler.report_status(current)
    assert handler.snapshot()["surfaces"]["wallpaper"]["revision"] == store.revision


@pytest.mark.parametrize("surface", ["render", "wallpaper"])
def test_host_surface_stop_reports_unloaded_and_rejects_late_ready(tmp_path, monkeypatch, surface):
    from types import SimpleNamespace
    from server.handlers.render_handler import RenderHandler
    from server.handlers.wallpaper_handler import WallpaperHandler
    from server.protocol import Method
    store, profile, _, _ = configured_store(tmp_path)
    presentation = CharacterPresentationCoordinator(lambda *_: None, emit_now=lambda *_: None)
    monkeypatch.setattr("server.handlers.visual_handler.coordinator", presentation)
    monkeypatch.setattr("server.handlers.visual_handler.bus.emit_now", lambda *_: None)
    async def emit(*_):
        pass
    monkeypatch.setattr("server.handlers.wallpaper_handler.bus.emit", emit)
    lifecycle = RenderHandler() if surface == "render" else WallpaperHandler()
    visual = VisualHandler()
    visual.configure(store, lambda: None, surface_active=lambda _: lifecycle.is_running())
    if surface == "render":
        lifecycle.configure(tmp_path, render_bridge=object(), character_closed=visual.surface_closed)
        stop = Method.RENDER_STOP
    else:
        lifecycle._subscribed = True
        lifecycle.configure(tmp_path, character_closed=visual.surface_closed)
        lifecycle._wallpaper_host = SimpleNamespace(stop=lambda: None)
        stop = Method.WALLPAPER_STOP
    receipt = {"surface": surface, "profile_id": profile["profile_id"], "runtime_id": store.runtime_id,
               "revision": store.revision, "state": "ready"}
    assert visual.report_status(receipt)
    assert asyncio.run(lifecycle.handle(stop, {}))["status"] == "stopped"
    assert visual.snapshot()["surfaces"][surface]["state"] == "unloaded"
    assert not visual.report_status(receipt)
    assert visual.snapshot()["surfaces"][surface]["state"] == "unloaded"
