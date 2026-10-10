"""Bounded source guardrails; findings name the owning boundary and repair."""
import json

from tools.maintainability_ratchet import (
    ROOT, WRITE_EXCEPTIONS, additions, inspect_source, inventory,
)


def test_no_new_upward_import_edges():
    baseline = json.loads((ROOT / "tests/fixtures/maintainability_baseline.json").read_text(encoding="utf-8"))
    actual = {f: v["imports"] for f, v in inventory().items() if v["imports"]}
    assert not additions(actual, baseline["imports"]), (
        "Move shared contracts to their owning lower layer; new upward imports: "
        + repr(additions(actual, baseline["imports"]))
    )


def test_persistent_replacements_use_the_shared_commit_boundary():
    hits = {(f, w["owner"], w["kind"]) for f, v in inventory().items() for w in v["writes"]}
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
