"""Bounded source guardrails; findings name the owning boundary and repair."""
import json

import pytest

from tools.maintainability_ratchet import (
    ROOT, WRITE_EXCEPTIONS, additions, inspect_source, inventory,
)


@pytest.fixture(scope="module")
def source_inventory():
    # Share one immutable repository snapshot within this test module. The CLI
    # and callers scanning an edited tree still get a fresh inventory each time.
    return inventory()


def test_no_new_upward_import_edges(source_inventory):
    baseline = json.loads((ROOT / "tests/fixtures/maintainability_baseline.json").read_text(encoding="utf-8"))
    actual = {f: v["imports"] for f, v in source_inventory.items() if v["imports"]}
    assert not additions(actual, baseline["imports"]), (
        "Move shared contracts to their owning lower layer; new upward imports: "
        + repr(additions(actual, baseline["imports"]))
    )


def test_persistent_replacements_use_the_shared_commit_boundary(source_inventory):
    hits = {(f, w["owner"], w["kind"]) for f, v in source_inventory.items() for w in v["writes"]}
    assert not hits - WRITE_EXCEPTIONS.keys(), (
        "Persistent state must use config.durable_io.write_text/write_bytes; "
        f"review owner-specific exceptions: {sorted(hits - WRITE_EXCEPTIONS.keys())}"
    )
    assert not WRITE_EXCEPTIONS.keys() - hits, "Remove obsolete state-write exceptions"
    assert all(WRITE_EXCEPTIONS.values())


def test_scanner_covers_aliases_relative_imports_and_concrete_keys():
    observed = inspect_source('''
import os as process
from os import getenv as env
from server.protocol import Method
from server import interaction_branch
from ..server import provider_branch
env("A")
process.environ.get("B")
process.environ["C"]
"D" in process.environ
"E" not in process.environ
process.environ["OUTPUT"] = "not a read"
''', "llm/example.py")
    assert observed["environment"] == ["A", "B", "C", "D", "E"]
    assert observed["imports"] == ["server.interaction_branch", "server.provider_branch"]
    assert additions({"same.py": ["new"]}, {"same.py": ["old"]}) == {"same.py": ["new"]}
    assert additions({"new.py": ["old"]}, {"same.py": ["old"]}) == {"new.py": ["old"]}


def test_state_write_scanner_distinguishes_exclusive_creation_and_string_replacement():
    observed = inspect_source('''
import json
from os import replace as publish
def save(path, temporary):
    with open(path, "w") as output:
        json.dump({}, output)
    with path.open("wb") as output:
        json.dump({}, output)
    with open(path, "x") as output:
        json.dump({}, output)
    path.write_text(json.dumps({}) + "\\n")
    temporary.replace(path)
    publish(temporary, path)
    path.replace("\\\\", "/")
''', "core/example.py")
    assert [w["kind"] for w in observed["writes"]] == ["json_dump", "json_dump", "json_text", "replace", "replace"]


def test_environment_helpers_preserve_keys_through_nested_and_keyword_calls():
    observed = inspect_source('''
import os as process
def read(default, key):
    return process.getenv(key, default)
def flag(*, name):
    return read(False, key=name)
flag(name="NEW_MODULE_KEY")
async def startup():
    def read(key):
        return process.environ.get(key)
    def allowed():
        return read("NEW_NESTED_KEY")
    return allowed()
def other():
    def read(key):
        return key
    return read("NOT_AN_ENVIRONMENT_KEY")
def injected(read):
    return read("UNKNOWN_CALLBACK")
''', "server/example.py")
    assert observed["environment"] == ["NEW_MODULE_KEY", "NEW_NESTED_KEY"]
    assert additions({"server/example.py": observed["environment"]}, {}) == {
        "server/example.py": ["NEW_MODULE_KEY", "NEW_NESTED_KEY"],
    }


@pytest.mark.parametrize("expression", ["process.getenv(key)", "process.environ[key]", "key in process.environ"])
def test_environment_helper_detection_depends_on_parameter_use(expression):
    observed = inspect_source(f'''
import os as process
def any_name(key):
    return {expression}
any_name("NEW_KEY")
def _bool_env(key):
    return bool(key)
_bool_env("NOT_A_KEY")
''', "server/example.py")
    assert observed["environment"] == ["NEW_KEY"]


def test_module_value_replacements_are_not_persistent_writes():
    observed = inspect_source('''
import dataclasses
import dataclasses as dc
import copy as values
import os as process
from dataclasses import replace as evolve
def update(state, temporary, path):
    dataclasses.replace(state, revision=1)
    dc.replace(state, revision=2)
    values.replace(state, revision=3)
    evolve(state, revision=4)
    temporary.replace(target=path)
    process.replace(temporary, path)
''', "core/example.py")
    assert [(w["owner"], w["kind"]) for w in observed["writes"]] == [("update", "replace"), ("update", "replace")]


def test_inventory_observes_edits_between_calls(tmp_path):
    directory = tmp_path / "server"
    directory.mkdir()
    source = directory / "example.py"
    source.write_text('import os\nos.getenv("BEFORE")', encoding="utf-8")
    assert inventory(tmp_path)["server/example.py"]["environment"] == ["BEFORE"]
    source.write_text('import os\nos.getenv("AFTER")', encoding="utf-8")
    assert inventory(tmp_path)["server/example.py"]["environment"] == ["AFTER"]


def test_silent_exception_report_is_observational_and_respects_explanation():
    observed = inspect_source('''
try: work()
except Exception: pass
try: work()
except Exception:  # best-effort: optional cleanup
    pass
try: work()
except Exception as exc: report(exc)
''', "core/example.py")
    assert [v["explained"] for v in observed["silent"]] == [False, True]
