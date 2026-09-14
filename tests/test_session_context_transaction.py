"""Real Work context writes, including offline Control Ledger receipt joins.

No live dispatcher/cohort, model, Provider, EventBus or presentation is involved.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerNotFound,
    WorkLedgerStore,
)
from server.control_ledger import ControlEffect, ControlLedgerConflict, ControlLedgerStore
from server.work_destination_service import WorkDestinationService


@pytest.fixture
def context(tmp_path, monkeypatch):
    from config import settings

    scratch = tmp_path / "scratch"
    draft_root = scratch / "draft"
    draft_root.mkdir(parents=True)
    monkeypatch.setattr(settings, "WORK_SCRATCH_ROOT", str(scratch))
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
    path = tmp_path / "ledger.sqlite3"
    with WorkLedgerStore(path) as store:
        a, b = [store.create_or_get_project(root) for root in roots]
        items = [
            store.create_work_item(project.project_id, title=project.name) for project in (a, b)
        ]
        scratch_project = store.create_or_get_project(scratch)
        draft = store.create_work_item(
            scratch_project.project_id, title="Draft", workspace_path=draft_root
        )
        external = store.create_work_item(
            a.project_id, title="Workspace-less", workspace_mode="none"
        )
        destination = WorkDestinationService(store, registry_check=lambda _: True)
        destination.bind_session_context("voice", a.project_id, work_item_id=items[0].work_item_id)
        destination.set_session_project_feedback("voice", status="info", message="prior fact")
        yield SimpleNamespace(
            path=path,
            store=store,
            destination=destination,
            a=a,
            b=b,
            item_a=items[0],
            item_b=items[1],
            draft=draft,
            external=external,
        )


def rows(context):
    return (
        context.store.get_conversation_binding("voice"),
        context.store.get_session_work_context("voice"),
    )


def apply_choice(context, choice):
    if choice == "clear":
        context.destination.clear_session_project("voice")
    elif choice == "project":
        context.destination.set_session_project("voice", context.b.project_id)
    else:
        item = getattr(context, choice)
        context.destination.bind_session_context("voice", "", work_item_id=item.work_item_id)


@pytest.mark.parametrize("choice", ["project", "item_b", "draft", "external", "clear"])
def test_all_context_write_sets_preserve_existing_semantics(context, choice):
    original_binding, _ = rows(context)
    apply_choice(context, choice)
    binding, active = rows(context)
    if choice == "clear":
        assert binding is None and active is None
    elif choice == "project":
        assert binding.project_id == context.b.project_id
        assert binding.binding_kind == "project" and not binding.anchor_work_item_id
        assert active is None
    elif choice == "item_b":
        assert binding.project_id == context.b.project_id
        assert (
            binding.anchor_work_item_id == active.active_work_item_id == context.item_b.work_item_id
        )
    else:
        # A foreground Draft does not erase or rewrite the standing Project row.
        assert binding == original_binding
        assert active.active_work_item_id == getattr(context, choice).work_item_id
        assert context.destination.session_project("voice") == context.a.project_id
    assert "voice" not in context.destination._session_project_feedback


@pytest.mark.parametrize("choice", ["project", "item_b", "clear"])
def test_second_write_failure_preserves_both_rows_and_feedback(context, choice):
    before = rows(context)
    feedback = dict(context.destination._session_project_feedback)
    operation = "UPDATE" if choice == "item_b" else "DELETE"
    context.store._connection.execute(
        f"CREATE TRIGGER fail_second BEFORE {operation} ON session_work_contexts "
        "BEGIN SELECT RAISE(ABORT, 'injected second-write failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="second-write"):
        apply_choice(context, choice)
    assert rows(context) == before
    assert context.destination._session_project_feedback == feedback
    assert context.destination.session_project("voice") == context.a.project_id


def test_service_reads_new_durable_binding_instead_of_invalidating_a_cached_project(context):
    assert context.destination.session_project("voice") == context.a.project_id
    with WorkLedgerStore(context.path) as other:
        WorkDestinationService(other, registry_check=lambda _: True).set_session_project(
            "voice", context.b.project_id
        )
    context.destination._registry_check = lambda path: path == context.b.canonical_path
    assert context.destination.session_project("voice") == context.b.project_id
    assert context.store.get_conversation_binding("voice").project_id == context.b.project_id


@pytest.mark.parametrize("replacement_available", [False, True])
def test_availability_cleanup_cannot_delete_a_replacement_it_did_not_validate(
    context, replacement_available
):
    other = WorkDestinationService(context.store, registry_check=lambda _: True)
    checked = []

    def check(path):
        checked.append(path)
        if path == context.a.canonical_path:
            # Deterministically place another writer at the read/check/delete
            # boundary, without depending on thread timing.
            other.set_session_project("voice", context.b.project_id)
            return False
        return replacement_available

    context.destination._registry_check = check
    result = context.destination.session_project("voice")
    assert checked == [context.a.canonical_path, context.b.canonical_path]
    assert result == (context.b.project_id if replacement_available else "")
    binding = context.store.get_conversation_binding("voice")
    if replacement_available:
        assert binding.project_id == context.b.project_id
        # Losing cleanup did not author a rejection or clear unrelated feedback.
        assert context.destination._session_project_feedback["voice"] == {
            "status": "info",
            "message": "prior fact",
        }
    else:
        assert binding is None
        assert context.destination._session_project_feedback["voice"]["status"] == "rejected"


def test_context_writers_preserve_timestamps_and_shallow_metadata_merge(context):
    store = context.store
    store._clock = lambda: 100.0
    store.update_session_context(
        "metadata",
        project_id=context.a.project_id,
        work_item_id=context.item_a.work_item_id,
        binding_metadata={"keep": 1, "nested": {"old": 1}},
        work_metadata={"keep": 2, "nested": {"old": 2}},
    )
    store._clock = lambda: 200.0
    store.update_session_context(
        "metadata",
        project_id=context.b.project_id,
        work_item_id=context.item_b.work_item_id,
        binding_metadata={"source": "focus", "nested": {"new": 1}},
        work_metadata={"source": "context_binding", "nested": {"new": 2}},
    )
    binding = store.get_conversation_binding("metadata")
    active = store.get_session_work_context("metadata")
    assert binding.created_at == active.created_at == 100.0
    assert binding.updated_at == active.updated_at == 200.0
    assert binding.metadata == {"keep": 1, "source": "focus", "nested": {"new": 1}}
    assert active.metadata == {"keep": 2, "source": "context_binding", "nested": {"new": 2}}


@pytest.mark.parametrize("invalid", ["project", "work", "cross_project"])
def test_atomic_context_writer_keeps_exact_entity_ownership(context, invalid):
    before = rows(context)
    project = "missing" if invalid == "project" else context.b.project_id
    work = "missing" if invalid == "work" else context.item_a.work_item_id
    with pytest.raises((WorkLedgerConflict, WorkLedgerNotFound)):
        context.store.update_session_context("voice", project_id=project, work_item_id=work)
    assert rows(context) == before


def test_sql_helper_requires_and_uses_the_callers_transaction(context):
    with closing(sqlite3.connect(context.path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        with closing(db.cursor()) as cursor:
            with pytest.raises(WorkLedgerConflict, match="active transaction"):
                WorkLedgerStore.write_session_context(cursor, "voice", project_id="", now=1.0)
            cursor.execute("BEGIN IMMEDIATE")
            # The new Project exists only inside this transaction. An accidental
            # read through the ordinary Store connection cannot find it.
            cursor.execute(
                "INSERT INTO projects SELECT 'uncommitted', name, display_path, canonical_path || '/new', "
                "path_identity || '/new', created_at, updated_at, metadata_json, state FROM projects WHERE project_id=?",
                (context.a.project_id,),
            )
            WorkLedgerStore.write_session_context(
                cursor, "voice", project_id="uncommitted", now=1.0
            )
            assert (
                cursor.execute(
                    "SELECT project_id FROM conversation_bindings WHERE session_id='voice'"
                ).fetchone()[0]
                == "uncommitted"
            )
            assert (
                context.store.get_conversation_binding("voice").project_id == context.a.project_id
            )
            db.rollback()
    assert context.store.get_project("uncommitted") is None
    assert context.store.get_conversation_binding("voice").project_id == context.a.project_id


def accepted_focus(ledger, project_id):
    ledger.admit(
        root_id="root",
        source_scope="chat:voice",
        fence_scope="foreground",
        utterance_id="utterance",
        chat_epoch=1,
        authority_mode="turn_decision",
        transcript_hash="offline exact focus fixture",
    )
    ledger.accept(
        "root",
        chat_epoch=1,
        plan_id="plan",
        effects=(
            ControlEffect(
                "effect",
                "focus",
                "session:voice",
                {"session_id": "voice", "project_id": project_id},
            ),
        ),
        evidence={"source": "offline grounded fixture, not a live sealer"},
    )


def focus_sql(cursor, payload):
    WorkLedgerStore.write_session_context(
        cursor,
        payload["session_id"],
        project_id=payload["project_id"],
        now=300.0,
        binding_metadata={"source": "offline_control_focus"},
    )
    return dict(payload)


@pytest.mark.parametrize("clear", [False, True])
def test_real_focus_domain_and_receipt_commit_once_without_creating_work(context, clear):
    target = "" if clear else context.b.project_id
    original_work = context.store.list_work_items()
    with closing(ControlLedgerStore(context.path)) as ledger:
        accepted_focus(ledger, target)
        results = []

        def apply(cursor, payload):
            results.append(payload)
            return focus_sql(cursor, payload)

        first = ledger.apply_local("effect", owner="offline-focus", apply=apply)
        assert first["receipt"]["details"] == {"session_id": "voice", "project_id": target}
        assert ledger.get_effect("effect")["state"] == "terminal"
        assert context.destination.session_project("voice") == target
        assert context.store.get_session_work_context("voice") is None
        # Replaying an old receipt after a later ordinary binding must not put
        # the old Project back into either durable state or a private cache.
        context.destination.set_session_project("voice", context.a.project_id)
        replay = ledger.apply_local("effect", owner="offline-focus", apply=apply)
        assert replay["replayed"] and replay["receipt"] == first["receipt"]
        assert len(results) == 1
        assert context.destination.session_project("voice") == context.a.project_id
    assert context.store.list_work_items() == original_work


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("boundary", ["second_write", "receipt"])
def test_real_focus_failure_rolls_back_domain_claim_and_receipt(context, clear, boundary):
    before = rows(context)
    with closing(ControlLedgerStore(context.path)) as ledger:
        accepted_focus(ledger, "" if clear else context.b.project_id)
        trigger = (
            "BEFORE DELETE ON session_work_contexts"
            if boundary == "second_write"
            else "BEFORE INSERT ON control_effect_receipts"
        )
        context.store._connection.execute(
            f"CREATE TRIGGER fail_domain {trigger} BEGIN SELECT RAISE(ABORT, 'injected domain failure'); END"
        )
        with pytest.raises(ControlLedgerConflict, match="domain failure"):
            ledger.apply_local("effect", owner="offline-focus", apply=focus_sql)
        assert rows(context) == before
        assert ledger.get_effect("effect")["state"] == "pending"
        assert ledger.get_receipt("effect") is None


def test_concurrent_real_focus_apply_has_one_domain_and_receipt_commit(context):
    with (
        closing(ControlLedgerStore(context.path)) as first,
        closing(ControlLedgerStore(context.path)) as second,
    ):
        accepted_focus(first, context.b.project_id)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda index: (first if index % 2 else second).apply_local(
                        "effect", owner="offline-focus", apply=focus_sql
                    ),
                    range(16),
                )
            )
        assert sum(not result["replayed"] for result in results) == 1
        assert context.destination.session_project("voice") == context.b.project_id
        assert context.store.get_session_work_context("voice") is None


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("boundary", ["second_write", "before_receipt", "after_commit"])
def test_process_death_recovers_real_focus_at_the_receipt_boundary(context, clear, boundary):
    before = rows(context)
    with closing(ControlLedgerStore(context.path)) as ledger:
        accepted_focus(ledger, "" if clear else context.b.project_id)
    script = r"""
import os, sys
from pathlib import Path
from agent_host.work_ledger_store import WorkLedgerStore
from server.control_ledger import ControlLedgerStore
ledger = ControlLedgerStore(Path(sys.argv[1]))
boundary = sys.argv[2]
def apply(cursor, payload):
    if boundary == "second_write":
        cursor.connection.create_function("die", 0, lambda: os._exit(77))
        cursor.execute("CREATE TEMP TRIGGER die_second BEFORE DELETE ON session_work_contexts BEGIN SELECT die(); END")
    WorkLedgerStore.write_session_context(cursor, payload["session_id"], project_id=payload["project_id"], now=300.0)
    if boundary == "before_receipt":
        os._exit(77)
    return payload
ledger.apply_local("effect", owner="offline-focus", apply=apply)
os._exit(77)
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(context.path), boundary],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 77, result.stdout + result.stderr
    with closing(ControlLedgerStore(context.path)) as ledger:
        committed = boundary == "after_commit"
        assert ledger.get_effect("effect")["state"] == ("terminal" if committed else "pending")
        assert bool(ledger.get_receipt("effect")) is committed
        if not committed:
            assert rows(context) == before
        replay = ledger.apply_local("effect", owner="offline-focus", apply=focus_sql)
        assert replay["replayed"] is committed
        assert context.destination.session_project("voice") == (
            "" if clear else context.b.project_id
        )
        assert context.store.get_session_work_context("voice") is None
