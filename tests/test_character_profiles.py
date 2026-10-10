"""User character persistence, fresh preview and immutable startup identity."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tomllib

import pytest

from config import settings
from config import durable_io
from core import character_profiles as profiles
from llm import character_prompts as characters
from server.handlers.character_handler import CharacterHandler
from server.protocol import Method


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CHARACTER_DIR", str(tmp_path))
    return profiles.CharacterStore(tmp_path)


def test_default_directory_is_project_local_and_override_is_independent(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CHARACTER_DIR", ".amadeus/characters")
    assert profiles.user_character_directory() == ROOT / ".amadeus/characters"
    monkeypatch.setattr(settings, "CHARACTER_DIR", str(tmp_path))
    assert profiles.user_character_directory() == tmp_path


@pytest.mark.parametrize("name", ["Mira", "牧瀬 紅莉栖", "你好 🌟", "Alpha / Beta", 'A ${name} \"quoted\" name'])
def test_host_generates_ascii_identity_and_rename_preserves_it(store, name):
    role = store.create(name=name)
    character_id = role["character_id"]
    assert characters.validate_character_id(character_id) == character_id
    assert character_id.startswith("character-") and character_id.isascii()
    assert role["name"] == name and role["persona"] == ""
    assert role["valid"] and role["editable"] and not role["builtin"]
    path = store.directory / f"{character_id}.toml"
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {
        "persona": "", "names": {"character_id": character_id, "name": name}}
    updated = store.update(character_id, name="Renamed", persona="Friendly and concise.")
    assert updated["character_id"] == character_id and updated["name"] == "Renamed"
    assert store.create(name="Renamed")["character_id"] != character_id


@pytest.mark.parametrize("name,persona", [
    (None, ""), (False, ""), ("", ""), (" ", ""), (" Mira", ""), ("Mira ", ""),
    ("Mira", None), ("Mira", False), ("Mira", "bad\0text"), ("bad\0name", ""),
    ("Mira", "x" * 8193), ("x" * 8193, ""), ("Mira", "\ud800"),
])
def test_invalid_full_document_never_replaces_saved_bytes(store, name, persona):
    role = store.create(name="Original")
    path = store.directory / f"{role['character_id']}.toml"
    original = path.read_bytes()
    with pytest.raises(ValueError):
        store.update(role["character_id"], name=name, persona=persona)
    assert path.read_bytes() == original
    with pytest.raises(ValueError):
        store.create(name=name, persona=persona)
    assert list(store.directory.iterdir()) == [path]


@pytest.mark.parametrize("stage", ["fsync", "replace"])
def test_failed_atomic_write_preserves_original_and_removes_temporary(store, monkeypatch, stage):
    role = store.create(name="Original")
    path = store.directory / f"{role['character_id']}.toml"
    original = path.read_bytes()
    def fail(*_args):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(durable_io.os, stage, fail)
    with pytest.raises(OSError, match="synthetic write failure"):
        store.update(role["character_id"], name="Changed")
    assert path.read_bytes() == original
    assert list(store.directory.iterdir()) == [path]


def test_replace_sees_fsynced_complete_document_in_same_directory(store, monkeypatch):
    observed = []
    original_fsync, original_replace = durable_io.os.fsync, durable_io.os.replace
    def synced(descriptor):
        original_fsync(descriptor)
        observed.append("fsync")
    def replace(source, destination):
        assert observed == ["fsync"]
        assert Path(source).parent == Path(destination).parent == store.directory
        assert tomllib.loads(Path(source).read_text(encoding="utf-8"))["names"]["name"] == "Atomic"
        observed.append("replace")
        original_replace(source, destination)
    monkeypatch.setattr(durable_io.os, "fsync", synced)
    monkeypatch.setattr(durable_io.os, "replace", replace)
    assert store.create(name="Atomic")["valid"]
    assert observed[:2] == ["fsync", "replace"]


def test_builtin_cannot_be_shadowed_or_updated(store):
    (store.directory / "kurisu.toml").write_text('[names]\ncharacter_id="kurisu"\nname="Impostor"\n', encoding="utf-8")
    before = (store.directory / "kurisu.toml").read_bytes()
    assert store.list() == [{"character_id": "kurisu", "name": "Kurisu", "persona": "",
        "builtin": True, "valid": True, "editable": False, "pending_restart": False, "edit_error": "Built-in characters are read-only."}]
    assert characters.load_fresh("kurisu", directory=store.directory).values["short_name"] == "Kurisu"
    with pytest.raises(ValueError, match="read-only"):
        store.update("kurisu", name="Impostor", persona="Invented authority")
    assert (store.directory / "kurisu.toml").read_bytes() == before


def test_only_builtin_is_read_from_shipped_resources(store, monkeypatch):
    monkeypatch.setattr(characters, "files", lambda _package: store.directory)
    (store.directory / "packaged-only.toml").write_text('[names]\ncharacter_id="packaged-only"\nname="Manual"\n', encoding="utf-8")
    with pytest.raises(profiles.CharacterNotFoundError):
        characters.load_fresh("packaged-only", directory=store.directory / "user")


@pytest.mark.parametrize("extra", [
    'display_name="Explicit"\n',
    '[texts]\nja_identity="Explicit identity"\n',
    '[voice_lines]\nvn_voice_choice="Explicit commentary"\n',
    '[ui]\nui_name="Explicit UI"\n',
])
def test_advanced_manual_roles_stay_loadable_and_cannot_be_rewritten(store, extra):
    path = store.directory / "manual.toml"
    path.write_text('[names]\ncharacter_id="manual"\nname="Manual"\n' + extra, encoding="utf-8")
    original = path.read_bytes()
    record = store.validate("manual")
    assert record["valid"] and not record["editable"]
    assert "edit its TOML file on disk" in record["edit_error"]
    with pytest.raises(ValueError, match="advanced fields"):
        store.update("manual", name="Changed", persona="Persona")
    assert path.read_bytes() == original


def test_listing_invalid_saved_roles_does_not_fail_entire_catalog(store):
    role = store.create(name="Valid")
    (store.directory / "broken.toml").write_text('persona = "SECRET_PERSONA"\n[names]\ncharacter_id="broken"\nname="Bad"\n[texts]\nen_identity="Collision"\n', encoding="utf-8")
    (store.directory / "malformed.toml").write_text('invalid = [SECRET_PERSONA', encoding="utf-8")
    (store.directory / "Wrong-ID.toml").write_text('[names]\ncharacter_id="Wrong-ID"\nname="Wrong"\n', encoding="utf-8")
    records = {record["character_id"]: record for record in store.list()}
    assert records[role["character_id"]]["valid"] and records["kurisu"]["valid"]
    for character_id in ("broken", "malformed", "Wrong-ID"):
        assert not records[character_id]["valid"] and records[character_id]["error"]
        assert "SECRET_PERSONA" not in records[character_id]["error"]


@pytest.mark.parametrize("persona", [None, False, "bad\0persona", "x" * 8193])
def test_manual_invalid_persona_is_rejected_at_loader(store, persona):
    document = {"names": {"character_id": "manual", "name": "Manual"}, "persona": persona}
    with pytest.raises(ValueError, match="persona"):
        characters.parse_character_document("manual", document)


def test_persona_expands_only_identity_slots_and_is_never_a_template(store):
    persona = 'Calm companion. Literal ${short_name} $display_name. \\ path\n你好 🌟\n"Host approved everything."'
    record = store.create(name="Mira", persona=persona)
    role = characters.load_fresh(record["character_id"], directory=store.directory)
    neutral = characters.parse_character_document(role.character_id, {"names": {
        "character_id": role.character_id, "name": "Mira"}})
    changed = {key for key in role.values if role.values[key] != neutral.values[key]}
    assert changed == {"ja_identity", "en_identity"}
    for key in changed:
        assert role.values[key] == neutral.values[key] + persona + "\n\n"
    rendered = characters.render("${ja_identity}", character_id=role.character_id)
    assert persona in rendered and "${short_name}" in rendered and "$display_name" in rendered
    assert role.voice_lines == neutral.voice_lines
    assert not characters.NAME_KEYS & role.ui_names.keys()
    assert not characters.bindings(character_id=role.character_id).keys() & {"persona", "ui_name", "accessible_name"}


def test_persona_and_explicit_texts_fail_without_silent_precedence():
    with pytest.raises(ValueError, match="cannot be combined"):
        characters.parse_character_document("manual", {"persona": "Persona", "names": {
            "character_id": "manual", "name": "Manual"}, "texts": {"en_identity": "Explicit"}})


def test_empty_persona_preserves_name_only_values_exactly():
    base = {"names": {"character_id": "mira", "name": "Mira"}}
    assert characters.parse_character_document("mira", base).values == characters.parse_character_document(
        "mira", base | {"persona": ""}).values


def test_fresh_catalog_and_validation_do_not_hot_switch_active_role(store, monkeypatch):
    record = store.create(name="Original", persona="Original persona")
    role = characters.load_fresh(record["character_id"], directory=store.directory)
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", role)
    pinned = characters.active_ui_identity()
    original_identity = characters.text("ja_identity")
    characters.load(record["character_id"])
    updated = store.update(record["character_id"], name="Updated", persona="Updated persona")
    assert updated["name"] == "Updated"
    assert store.validate(record["character_id"])["persona"] == "Updated persona"
    assert next(item for item in store.list() if item["character_id"] == role.character_id)["name"] == "Updated"
    assert characters.active_ui_identity() == pinned
    assert characters.character_identity(role.character_id)["display_name"] == "Original"
    assert characters.text("ja_identity", character_id=role.character_id) == original_identity
    assert characters.active_character() is role
    characters.load.cache_clear()
    assert characters.text("ja_identity", character_id=role.character_id) == original_identity
    assert characters.active_ui_identity() == pinned
    path = store.directory / f"{role.character_id}.toml"
    path.write_text('broken = [', encoding="utf-8")
    assert not store.validate(role.character_id)["valid"]
    assert characters.active_ui_identity() == pinned
    characters.load.cache_clear()


def test_builtin_ui_projection_preserves_exact_labels_without_prompt_pollution(monkeypatch):
    role = characters.load_fresh("kurisu")
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", role)
    assert characters.active_ui_identity() == {"character_id": "kurisu", "name": "Kurisu",
        "display_name": "Makise Kurisu (牧瀬紅莉栖)", "short_name": "Kurisu",
        "ui_name": "牧瀬 紅莉栖", "accessible_name": "牧濑红莉栖"}
    assert role.values.keys() == characters.NAME_KEYS | characters.TEXT_KEYS
    with pytest.raises(TypeError):
        role.ui_names["ui_name"] = "Changed"


@pytest.mark.asyncio
async def test_character_handler_roundtrip_has_no_runtime_selection(store):
    handler = CharacterHandler(store)
    active = characters.active_ui_identity()
    created = await handler.handle(Method.CHARACTER_CREATE, {"name": "Mira", "persona": "Calm"})
    record = created["character"]
    listing = await handler.handle(Method.CHARACTER_LIST, {})
    assert listing["active"] == active and record in listing["characters"]
    assert await handler.handle(Method.CHARACTER_ACTIVE, {}) == active
    updated = await handler.handle(Method.CHARACTER_UPDATE, {"character_id": record["character_id"],
        "name": "Renamed", "persona": "Friendly"})
    assert updated["character"]["character_id"] == record["character_id"]
    assert await handler.handle(Method.CHARACTER_VALIDATE, {"character_id": record["character_id"]}) == updated
    assert characters.active_ui_identity() == active
    with pytest.raises(ValueError, match="invalid character id"):
        await handler.handle(Method.CHARACTER_VALIDATE, {"character_id": "../kurisu"})


def _startup(environment: dict[str, str], arguments: list[str]) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    env.update(environment)
    env["USERPROFILE"] = str(Path(environment["AMADEUS_CHARACTER_DIR"]) / "home")
    env["LOCALAPPDATA"] = str(Path(environment["AMADEUS_CHARACTER_DIR"]) / "appdata")
    env["PYTHONUTF8"] = "1"
    env["TTS_DEVICE"] = "cpu"
    return subprocess.run([sys.executable, "-X", "utf8", *arguments], cwd=ROOT,
        env=env, capture_output=True, encoding="utf-8", timeout=30)


@pytest.mark.parametrize("failure", ["missing", "malformed", "invalid", "persona_collision"])
def test_cli_selected_role_failure_exits_78_before_runtime_imports(tmp_path, failure):
    if failure != "missing":
        contents = {'malformed': 'secret = [SECRET_PERSONA',
            'invalid': '[names]\ncharacter_id="selected"\nname=""\n',
            'persona_collision': 'persona="SECRET_PERSONA"\n[names]\ncharacter_id="selected"\nname="Selected"\n[texts]\nen_identity="Explicit"\n'}[failure]
        (tmp_path / "selected.toml").write_text(contents, encoding="utf-8")
    result = _startup({"AMADEUS_CHARACTER_ID": "selected", "AMADEUS_CHARACTER_DIR": str(tmp_path)},
        ["-m", "server.app", "--port", "19877"])
    assert result.returncode == profiles.CHARACTER_LOAD_EXIT_CODE == 78, result.stderr
    assert "CHARACTER_LOAD_FAILED:" in result.stderr
    assert "SECRET_PERSONA" not in result.stderr and "Traceback" not in result.stderr
    assert not result.stdout


def test_library_import_raises_typed_selected_role_exception_without_exiting(tmp_path):
    result = _startup({"AMADEUS_CHARACTER_ID": "missing", "AMADEUS_CHARACTER_DIR": str(tmp_path)},
        ["-c", 'from core.character_profiles import CharacterLoadError\ntry:\n import server.app\nexcept CharacterLoadError:\n print("typed-character-error")\nelse:\n raise AssertionError("missing character accepted")'])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "typed-character-error"


def test_unrelated_settings_failure_is_not_character_failure(tmp_path):
    result = _startup({"AMADEUS_CHARACTER_ID": "missing", "AMADEUS_CHARACTER_DIR": str(tmp_path),
        "LLM_PROVIDER": "invalid-provider"}, ["-m", "server.app", "--port", "19877"])
    assert result.returncode != 78 and result.returncode != 0
    assert "LLM_PROVIDER" in result.stderr and "CHARACTER_LOAD_FAILED:" not in result.stderr


@pytest.mark.asyncio
async def test_active_http_endpoint_enforces_existing_auth_and_origin_without_booting_server():
    # Execute the production endpoint body without bootstrap's audio/network work.
    from fastapi import HTTPException
    from server.app import _http_request_authenticated, _http_request_origin_allowed
    from server.local_auth import LocalAuthPolicy

    tree = ast.parse((ROOT / "server/app.py").read_text(encoding="utf-8"))
    endpoint = next(node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "character_active")
    endpoint.decorator_list = []
    endpoint.args.args[0].annotation = None
    policy = LocalAuthPolicy(mode="required", token="synthetic-token", instance_nonce="synthetic-instance")
    namespace = {"HTTPException": HTTPException, "active_ui_identity": characters.active_ui_identity,
        "_http_request_authenticated": _http_request_authenticated,
        "_http_request_origin_allowed": _http_request_origin_allowed, "auth_policy": policy, "port": 19877}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[endpoint], type_ignores=[])), "<active-endpoint>", "exec"), namespace)
    call = namespace["character_active"]
    with pytest.raises(HTTPException) as rejected:
        await call(SimpleNamespace(headers={}))
    assert rejected.value.status_code == 401
    headers = {"x-amadeus-token": "synthetic-token"}
    with pytest.raises(HTTPException) as rejected:
        await call(SimpleNamespace(headers=headers | {"origin": "https://untrusted.invalid"}))
    assert rejected.value.status_code == 403
    assert await call(SimpleNamespace(headers=headers)) == characters.active_ui_identity()


def test_fixed_persona_bound_with_maximum_name_fails_before_any_replacement(store):
    name = "🌟" * 128
    record = store.create(name=name, persona="Original")
    path = store.directory / f"{record['character_id']}.toml"
    assert store.update(record["character_id"], name=name, persona="🌟" * 7900)["valid"]
    role = characters.load_fresh(record["character_id"], directory=store.directory)
    assert len(role.name) == 128 and len(role.persona) == 7900
    assert max(len(role.values[key]) for key in ("ja_identity", "en_identity")) < 8192
    valid_bytes = path.read_bytes()
    with pytest.raises(ValueError, match="7900 Unicode code points"):
        store.update(record["character_id"], name=name, persona="🌟" * 7901)
    assert path.read_bytes() == valid_bytes


@pytest.mark.parametrize("character_id", [None, "", "../role", "Kurisu", "app", "user", "system"])
def test_invalid_update_identity_never_touches_catalog(store, character_id):
    role = store.create(name="Original")
    path = store.directory / f"{role['character_id']}.toml"
    original = path.read_bytes()
    with pytest.raises(ValueError, match="invalid character id"):
        store.update(character_id, name="Changed")
    assert path.read_bytes() == original
    assert list(store.directory.iterdir()) == [path]


def test_failed_concurrent_save_cannot_replace_the_last_successful_save(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    role = store.create(name="Original")
    path = store.directory / f"{role['character_id']}.toml"
    replacing, second_started, release = Event(), Event(), Event()
    original_replace, original_fsync = durable_io.os.replace, durable_io.os.fsync
    failed_thread = []
    def replace(source, destination):
        replacing.set()
        assert release.wait(5)
        original_replace(source, destination)
    def fsync(descriptor):
        if failed_thread and current_thread().ident == failed_thread[0]:
            raise OSError("synthetic second-save failure")
        original_fsync(descriptor)
    def second_save():
        failed_thread.append(current_thread().ident)
        second_started.set()
        return profiles.CharacterStore(store.directory).update(role["character_id"], name="Failed")
    monkeypatch.setattr(durable_io.os, "replace", replace)
    monkeypatch.setattr(durable_io.os, "fsync", fsync)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(store.update, role["character_id"], name="Last successful")
        assert replacing.wait(5)
        second = executor.submit(second_save)
        assert second_started.wait(5)
        release.set()
        assert first.result(timeout=5)["name"] == "Last successful"
        with pytest.raises(OSError, match="second-save failure"):
            second.result(timeout=5)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["names"]["name"] == "Last successful"
    assert list(store.directory.iterdir()) == [path]


@pytest.mark.parametrize("literal", ["\x7f", '\x01\t\n\r\b\f\\"${name}你好 🌟', "\x7f你好 🌟\n$persona"])
def test_saved_persona_roundtrips_toml_controls_and_astral_unicode(store, literal):
    name, persona = 'Name 你好 🌟 $name "quoted" \\ end', f"Persona {literal} end"
    record = store.create(name=name, persona=persona)
    assert record["valid"] and record["name"] == name and record["persona"] == persona
    path = store.directory / f"{record['character_id']}.toml"
    saved = tomllib.loads(path.read_text(encoding="utf-8"))
    assert saved["names"]["name"] == name and saved["persona"] == persona
    role = characters.load_fresh(record["character_id"], directory=store.directory)
    assert role.name == name and role.persona == persona
    updated = store.update(record["character_id"], name=f"Updated {name}", persona=f"Updated {persona}")
    assert updated["valid"] and updated["name"] == f"Updated {name}" and updated["persona"] == f"Updated {persona}"
    reloaded = characters.load_fresh(record["character_id"], directory=store.directory)
    assert reloaded.name == updated["name"] and reloaded.persona == updated["persona"]
    if "\x7f" in literal:
        assert "\x7f" not in path.read_text(encoding="utf-8")
        assert r"\u007f" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name,message", [
    ("Mira\nSecond", "one line"), ("Mira\rSecond", "one line"),
    ("Mira\tSecond", "control characters"), ("Mira\x7fSecond", "control characters"),
    ("Mira\x85Second", "control characters"), ("Mira\u2028Second", "one line"),
    ("Mira\u2029Second", "one line"), ("🌟" * 129, "128 Unicode code points"),
    ("x" * 129, "128 Unicode code points"), ("\ud800", "valid Unicode"),
])
def test_names_reject_multiple_lines_controls_and_overlong_code_point_counts(store, name, message):
    record = store.create(name="Original")
    path = store.directory / f"{record['character_id']}.toml"
    original = path.read_bytes()
    for operation in (lambda: store.create(name=name),
            lambda: store.update(record["character_id"], name=name)):
        with pytest.raises(ValueError, match=message):
            operation()
    assert path.read_bytes() == original
    assert list(store.directory.iterdir()) == [path]


@pytest.mark.parametrize("key", sorted(characters.NAME_KEYS - {"character_id"}) + ["ui_name", "accessible_name"])
@pytest.mark.parametrize("invalid", ["Bypass\nName", "Bypass\x7fName", "🌟" * 129])
def test_explicit_name_and_ui_overrides_cannot_bypass_name_rules(key, invalid):
    document = {"names": {"character_id": "manual", "name": "Manual"}}
    document.setdefault("ui" if key in {"ui_name", "accessible_name"} else "names", {})[key] = invalid
    with pytest.raises(ValueError, match=key):
        characters.parse_character_document("manual", document)


@pytest.mark.parametrize("name", ["🌟" * 128, "你" * 128, "👩‍🔬" * 42 + "🌟🌟"])
def test_names_count_unicode_code_points_and_preserve_combined_unicode(store, name):
    assert len(name) == 128
    record = store.create(name=name)
    assert record["name"] == name and record["valid"]
    role = characters.load_fresh(record["character_id"], directory=store.directory)
    assert role.name == name and role.values["display_name"] == name


@pytest.mark.parametrize("persona", [" \t\r\n", "\u3000\u00a0", " \n Calm ${name} 🌟 \n\t ", " " * 8000])
def test_persona_strip_is_shared_by_saved_and_loaded_definition(store, persona):
    normalized = persona.strip()
    record = store.create(name="Mira", persona=persona)
    path = store.directory / f"{record['character_id']}.toml"
    saved = tomllib.loads(path.read_text(encoding="utf-8"))
    assert record["persona"] == saved["persona"] == normalized
    role = characters.load_fresh(record["character_id"], directory=store.directory)
    assert role.persona == normalized
    neutral = characters.parse_character_document(role.character_id, {"names": {
        "character_id": role.character_id, "name": "Mira"}})
    for key in ("ja_identity", "en_identity"):
        assert role.values[key] == neutral.values[key] + (normalized + "\n\n" if normalized else "")
    manual = characters.parse_character_document(role.character_id, {"persona": persona,
        "names": {"character_id": role.character_id, "name": "Mira"}})
    assert manual == role
    updated = store.update(role.character_id, name="Mira", persona="\n " + persona + " \n")
    assert updated["persona"] == normalized


def test_persona_limit_applies_after_strip_on_save_and_manual_load(store):
    persona = " \t" + "🌟" * 7900 + "\n "
    record = store.create(name="Mira", persona=persona)
    assert len(record["persona"]) == 7900
    manual = {"persona": persona, "names": {"character_id": record["character_id"], "name": "Mira"}}
    assert len(characters.parse_character_document(record["character_id"], manual).persona) == 7900
    manual["persona"] = " \t" + "🌟" * 7901 + "\n "
    with pytest.raises(ValueError, match="7900 Unicode code points"):
        characters.parse_character_document(record["character_id"], manual)


def test_whitespace_persona_is_empty_for_explicit_text_collision_and_name_only_output():
    names = {"character_id": "manual", "name": "Mira"}
    document = {"names": names, "persona": " \n\t ", "texts": {"ja_identity": "Explicit"}}
    assert characters.parse_character_document("manual", document).persona == ""
    empty = characters.parse_character_document("manual", {"names": names})
    whitespace = characters.parse_character_document("manual", {"names": names, "persona": " \n\t "})
    assert empty == whitespace


def _catalog_record(store, character_id):
    return next(record for record in store.list() if record["character_id"] == character_id)


def test_pending_restart_follows_changed_definition_reversion_and_restart(store, monkeypatch):
    record = store.create(name="Original", persona="Original persona")
    role = characters.load_fresh(record["character_id"], directory=store.directory)
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", role)
    original_text = characters.text("ja_identity")
    assert not _catalog_record(store, role.character_id)["pending_restart"]
    changed = store.update(role.character_id, name="Updated", persona="Updated persona")
    assert changed["pending_restart"] and _catalog_record(store, role.character_id)["pending_restart"]
    characters.load.cache_clear()
    assert characters.active_character() is role
    assert characters.text("ja_identity") == original_text
    reverted = store.update(role.character_id, name="Original", persona=" Original persona ")
    assert not reverted["pending_restart"] and not _catalog_record(store, role.character_id)["pending_restart"]
    store.update(role.character_id, name="Restarted", persona="New persona")
    restarted = characters.load_fresh(role.character_id, directory=store.directory)
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", restarted)
    assert not _catalog_record(store, role.character_id)["pending_restart"]
    assert characters.active_ui_identity()["name"] == "Restarted"
    assert "New persona" in characters.text("ja_identity")


def test_pending_restart_uses_parsed_definition_not_toml_spelling_or_comments(store, monkeypatch):
    record = store.create(name="Mira", persona="Calm persona")
    role = characters.load_fresh(record["character_id"], directory=store.directory)
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", role)
    path = store.directory / f"{role.character_id}.toml"
    path.write_text(f"# Comment-only edit and equivalent TOML ordering.\npersona = ' Calm persona '\n\n[names]\nname = 'Mira'\ncharacter_id = '{role.character_id}'\n", encoding="utf-8")
    assert not _catalog_record(store, role.character_id)["pending_restart"]
    with path.open("a", encoding="utf-8") as stream:
        stream.write('display_name = "Mira"\n[voice_lines]\nvn_voice_choice = "Changed commentary"\n')
    advanced = _catalog_record(store, role.character_id)
    assert advanced["valid"] and advanced["pending_restart"] and not advanced["editable"]
    assert characters.active_character() is role


def test_nonactive_disk_changes_do_not_request_runtime_restart(store):
    record = store.create(name="Inactive")
    updated = store.update(record["character_id"], name="Edited")
    assert not updated["pending_restart"]
    assert not _catalog_record(store, record["character_id"])["pending_restart"]


@pytest.mark.parametrize("failure", ["malformed", "invalid_name", "missing"])
def test_invalid_or_missing_active_definition_is_visible_without_changing_runtime(store, monkeypatch, failure):
    record = store.create(name="Original", persona="Original persona")
    role = characters.load_fresh(record["character_id"], directory=store.directory)
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", role)
    path = store.directory / f"{role.character_id}.toml"
    if failure == "missing":
        path.unlink()
    elif failure == "malformed":
        path.write_text('broken = [', encoding="utf-8")
    else:
        path.write_text(f'[names]\ncharacter_id="{role.character_id}"\nname=""\n', encoding="utf-8")
    invalid = _catalog_record(store, role.character_id)
    assert not invalid["valid"] and invalid["error"] and not invalid["pending_restart"]
    assert characters.active_character() is role
    assert characters.active_ui_identity()["name"] == "Original"
    assert "Original persona" in characters.text("ja_identity")


@pytest.mark.asyncio
async def test_character_list_projects_fixed_unicode_limits_for_ui(store):
    listing = await CharacterHandler(store).handle(Method.CHARACTER_LIST, {})
    assert listing["limits"] == {"name_max_chars": 128, "persona_max_chars": 7900}
