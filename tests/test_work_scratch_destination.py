"""Where work that names no project goes.

Runnable directly by tools/run_tests.py and compatible with pytest.
Every case pins the scratch root inside its own temp directory: the code under
test creates directories, and a suite that used the configured default would
write into the developer's checkout each time it ran.
"""

from __future__ import annotations

import os
import sys
import tempfile
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.scratch_workspace import ScratchUnavailable, is_scratch_path, slugify
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _with_scratch_root(path: Path):
    class _Guard:
        def __enter__(self) -> None:
            self.previous = settings.WORK_SCRATCH_ROOT
            settings.WORK_SCRATCH_ROOT = str(path)

        def __exit__(self, *_exc: Any) -> None:
            settings.WORK_SCRATCH_ROOT = self.previous

    return _Guard()


def _prepare(
    coordinator: WorkLedgerCoordinator,
    *,
    task: str,
    cwd: Path | None = None,
    mode: str = "agent",
    provider: str = "codex",
    session_id: str = "",
) -> tuple[ProviderRunRequest, str]:
    request = ProviderRunRequest(
        provider=provider,
        task=task,
        cwd=str(cwd) if cwd else "",
        mode=mode,
        metadata={
            "source": "test",
            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
            **({"session_id": session_id} if session_id else {}),
        },
    )
    prepared = coordinator.prepare_request(request)
    return prepared, str(prepared.metadata["work"]["work_item_id"])


def _finish(store: WorkLedgerStore, work_item_id: str) -> None:
    """Release the single-writer lease the way a finished run would."""

    attempt = store.list_attempts(work_item_id)[-1]
    store.update_attempt(attempt.attempt_id, execution_status="succeeded")
    store.release_writer_lease(attempt.attempt_id)


def test_work_naming_no_project_never_reaches_a_real_repository() -> None:
    """The failure this whole path exists to remove.

    "Build me a chess game" names nothing, and the intake used to fall through
    to Path.cwd() -- the server's launch directory, which is the user's own
    repository. The quarantined chess artifacts are what that looked like.
    """

    with tempfile.TemporaryDirectory(prefix="scratch_destination_") as temp:
        root = Path(temp)
        real_project = root / "amadeus"
        real_project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                # A registered project exists and is the only candidate, which
                # is exactly the shape that used to swallow unrouted work.
                _prepare(coordinator, task="Fix the chat view", cwd=real_project)
                prepared, item_id = _prepare(coordinator, task="Build a chess game")

                item = store.get_work_item(item_id)
                assert item is not None
                assert is_scratch_path(item.workspace_path)
                assert Path(item.workspace_path) != real_project
                assert real_project.resolve() not in Path(item.workspace_path).parents
                assert Path(prepared.cwd) == Path(item.workspace_path)
                assert Path(item.workspace_path).is_dir()
    print("ok: work that names no project never lands in a real repository")


def test_each_scratch_task_gets_its_own_repository() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_isolation_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, chess = _prepare(coordinator, task="Build a chess game")
                _, gomoku = _prepare(coordinator, task="Build a gomoku game")

            chess_item = store.get_work_item(chess)
            gomoku_item = store.get_work_item(gomoku)
            assert chess_item is not None and gomoku_item is not None
            # Sharing one directory would let two unrelated one-offs overwrite
            # each other, and would leave nothing separable to promote later.
            assert chess_item.workspace_path != gomoku_item.workspace_path
            assert (Path(chess_item.workspace_path) / ".git").exists()
            # The readable stem comes from what the user asked for; the id is
            # what actually keeps it unique.
            assert "chess" in Path(chess_item.workspace_path).name
            assert "gomoku" in Path(gomoku_item.workspace_path).name
            # Two writers, no lease conflict, because the workspaces differ.
            assert len(store.list_writer_leases(active_only=True)) == 2
    print("ok: every scratch task owns a private repository")


def test_scratch_is_a_project_row_but_never_a_routing_candidate() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_candidates_") as temp:
        root = Path(temp)
        real_project = root / "amadeus"
        real_project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _prepare(coordinator, task="Fix the chat view", cwd=real_project)
                for index in range(4):
                    _prepare(coordinator, task=f"One-off number {index}")

                # Scratch needs a project row to own the work items, but it is
                # not a destination the model chooses between: naming nothing
                # already selects it, so listing it would be a false choice.
                paths = {Path(row.canonical_path) for row in store.list_projects()}
                assert (root / "scratch").resolve() in paths
                candidates = coordinator.workspace_routing_context()["candidates"]
                assert len(candidates) == 1, candidates
                assert Path(candidates[0]["workspacePath"]) == real_project
    print("ok: scratch owns work items without becoming a routing choice")


def test_a_draft_becomes_a_destination_only_when_the_user_says_so() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_promote_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, chess = _prepare(coordinator, task="Build a chess game")
                item = store.get_work_item(chess)
                assert item is not None

                projected = coordinator._project_item(item)
                assert projected["isScratch"] is True
                assert projected["canPromoteToProject"] is True

                promoted = coordinator.promote_work_item_to_project(chess)
                assert Path(promoted["workspacePath"]) == Path(item.workspace_path)

                # The directory does not move, but the task is now filed under
                # the project it plainly belongs to: it ran in that directory,
                # and that directory is the project.
                assert Path(item.workspace_path).is_dir()
                assert store.get_work_item(chess).project_id == promoted["projectId"]
                assert promoted["refiledTasks"] == 1

                # It is now a destination the model can name and reuse.
                candidates = coordinator.workspace_routing_context()["candidates"]
                assert [Path(row["workspacePath"]) for row in candidates] == [
                    Path(item.workspace_path)
                ]
                assert candidates[0]["projectName"] == item.title
                reused = coordinator.resolve_workspace_route(
                    {"project_id": promoted["projectId"]}
                )
                assert reused["status"] == "resolved"
                assert Path(reused["cwd"]) == Path(item.workspace_path)

                # Offering the action again would offer something with nothing
                # left to do.
                assert (
                    coordinator._project_item(store.get_work_item(chess))[
                        "canPromoteToProject"
                    ]
                    is False
                )
    print("ok: promotion is one deliberate act, idempotent, and moves nothing")


def test_exported_draft_promotion_materializes_editable_source_and_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_export_promote_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, work_item_id = _prepare(
                    coordinator,
                    task="Build an endless-loop browser game",
                )
                item = store.get_work_item(work_item_id)
                assert item is not None
                workspace = Path(item.workspace_path)
                staged = workspace / ".amadeus" / "proposed_exports" / "attempt-a"
                staged.mkdir(parents=True)
                source = staged / "ETERNAL_LOOP.html"
                payload = b"<html><title>Endless Loop</title></html>"
                source.write_bytes(payload)
                desktop = root / "Desktop" / source.name
                desktop.parent.mkdir()
                desktop.write_bytes(payload)
                store.register_artifact(
                    work_item_id,
                    kind="business.export",
                    title="Exported ETERNAL_LOOP.html",
                    path=desktop,
                    status="approved",
                    sha256=hashlib.sha256(payload).hexdigest(),
                    metadata={
                        "relative_path": source.name,
                        "source_path": str(source),
                        "export_status": "approved",
                    },
                )

                promoted = coordinator.promote_work_item_to_project(work_item_id)

                assert promoted["projectName"] == "ETERNAL_LOOP"
                assert promoted["materializedFiles"] == ["ETERNAL_LOOP.html"]
                assert (workspace / "ETERNAL_LOOP.html").read_bytes() == payload
                candidate = coordinator.workspace_routing_context()["candidates"][0]
                assert candidate["projectName"] == "ETERNAL_LOOP"
                assert "Build an endless-loop browser game" in candidate["projectAliases"]
                assert "ETERNAL_LOOP.html" in candidate["projectAliases"]
                aliased = coordinator.add_project_alias(
                    promoted["projectId"], "endless game"
                )
                assert "endless game" in aliased["projectAliases"]
                assert "endless game" in coordinator.workspace_routing_context()[
                    "candidates"
                ][0]["projectAliases"]
                for index in range(8):
                    coordinator.add_project_alias(
                        promoted["projectId"], f"explicit alias {index}"
                    )
                bounded = coordinator.workspace_routing_context()["candidates"][0][
                    "projectAliases"
                ]
                assert len(bounded) == 8
                assert bounded[0] == "explicit alias 7"
    print("ok: promoted exports become editable Project sources with stable aliases")


def test_legacy_generated_project_identity_can_be_repaired_without_overwrite() -> None:
    with tempfile.TemporaryDirectory(prefix="legacy_project_identity_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, work_item_id = _prepare(
                    coordinator,
                    task="Improve the endless game monsters",
                )
                item = store.get_work_item(work_item_id)
                assert item is not None
                workspace = Path(item.workspace_path)
                staged = workspace / ".amadeus" / "proposed_exports" / "attempt-old"
                staged.mkdir(parents=True)
                source = staged / "ETERNAL_LOOP.html"
                payload = b"legacy approved snapshot"
                source.write_bytes(payload)
                external = root / "Desktop" / source.name
                external.parent.mkdir()
                external.write_bytes(payload)
                store.register_artifact(
                    work_item_id,
                    kind="business.export",
                    path=external,
                    status="approved",
                    sha256=hashlib.sha256(payload).hexdigest(),
                    metadata={"relative_path": source.name, "source_path": str(source)},
                )
                legacy = store.create_or_get_project(workspace, name=item.title)
                store.reassign_workspace_to_project(workspace, legacy.project_id)

                repaired = coordinator.repair_project_identity(legacy.project_id)

                assert repaired["projectName"] == "ETERNAL_LOOP"
                assert repaired["materializedFiles"] == ["ETERNAL_LOOP.html"]
                assert (workspace / "ETERNAL_LOOP.html").read_bytes() == payload
                metadata = store.get_project(legacy.project_id).metadata
                assert metadata["identity_version"] == 1
                assert item.title in metadata["semantic_aliases"]
    print("ok: legacy generated Project identity and source can be repaired safely")


def test_only_a_draft_can_be_promoted() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_promote_guard_") as temp:
        root = Path(temp)
        real_project = root / "amadeus"
        real_project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, project_task = _prepare(
                    coordinator, task="Fix the chat view", cwd=real_project
                )
                try:
                    coordinator.promote_work_item_to_project(project_task)
                    raise AssertionError("a project task must not be promotable")
                except WorkLedgerConflict:
                    pass
                assert (
                    coordinator._project_item(store.get_work_item(project_task))[
                        "canPromoteToProject"
                    ]
                    is False
                )
    print("ok: promotion refuses anything that is not a draft")


def test_scratch_failure_refuses_instead_of_choosing_a_real_directory() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_unavailable_") as temp:
        root = Path(temp)
        real_project = root / "amadeus"
        real_project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _prepare(coordinator, task="Fix the chat view", cwd=real_project)
                with patch(
                    "server.work_ledger_coordinator.ensure_scratch_root",
                    side_effect=ScratchUnavailable("disk is read-only"),
                ):
                    route = coordinator.resolve_workspace_route({})
                # Substituting the one registered project here would write a
                # one-off into a repository the user cares about, which is the
                # whole failure being removed.
                assert route["status"] == "invalid"
                assert route["reason"] == "scratch_unavailable"
                assert not route["cwd"]
    print("ok: an unusable scratch root refuses rather than picking a real repo")


def test_a_subdirectory_of_a_project_is_not_a_second_project() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_project_root_") as temp:
        root = Path(temp)
        real_project = root / "amadeus"
        nested = real_project / "server"
        nested.mkdir(parents=True)
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ), patch(
            "server.work_ledger_coordinator.project_registry_entries",
            return_value=[str(real_project)],
        ), patch(
            "server.work_ledger_coordinator.cwd_in_project_registry",
            return_value=True,
        ):
            coordinator = WorkLedgerCoordinator(store)
            _prepare(coordinator, task="Fix the chat view", cwd=real_project)
            _prepare(coordinator, task="Work inside the server directory", cwd=nested)

            # A subdirectory is not a second destination. Registering it as one
            # would put it in the candidate list forever -- the same
            # list-grows-with-history shape that made isolation unusable.
            project_paths = {
                Path(row.canonical_path)
                for row in store.list_projects()
                if not is_scratch_path(row.canonical_path)
            }
            assert project_paths == {real_project.resolve()}
            assert len(coordinator.workspace_routing_context()["candidates"]) == 1
    print("ok: a subdirectory of a project routes to the project, not beside it")


def test_a_draft_is_findable_for_wording_but_never_for_routing() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_other_session_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _prepare(coordinator, task="Build a chess game")

            # A later conversation finds nothing -- that is the design, and it
            # is why lookup carries no extra weight here.
            assert coordinator.conversation_work_items_by_file("other-session", "chess") == []

            # But the draft exists and is on screen, so the host can still say
            # something true about it. Wording only: this feeds no route.
            found = coordinator.drafts_in_other_conversations("chess")
            assert [row["title"] for row in found] == ["Build a chess game"]
            assert coordinator.drafts_in_other_conversations("nothing-like-this") == []
    print("ok: a draft outside the conversation can be described, never routed")


def test_a_kept_place_stays_answerable_in_a_later_conversation() -> None:
    """The whole of what keeping something buys, on the lookup side.

    Naming a project in a new session already routes correctly, and the agent
    reads the repository for content -- but "was that finished?" is a question
    only the ledger can answer, and it used to stop at the session boundary.
    """

    with tempfile.TemporaryDirectory(prefix="scratch_cross_session_") as temp:
        root = Path(temp)
        project = root / "amadeus"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, project_task = _prepare(
                    coordinator,
                    task="Add emoji.py to the chat view",
                    cwd=project,
                    session_id="monday",
                )
                _, draft = _prepare(
                    coordinator, task="Build chess.py", session_id="monday"
                )

            later = "friday"
            # A project's past is reachable from a later conversation...
            found = coordinator.conversation_work_items_by_file(
                later, "emoji.py", include_kept_projects=True
            )
            assert [row["work_item_id"] for row in found] == [project_task]
            # ...a draft's is not. That is what "draft" means, not a gap.
            assert coordinator.conversation_work_items_by_file(
                later, "chess.py", include_kept_projects=True
            ) == []

            # Keeping the draft is exactly what changes that.
            coordinator.promote_work_item_to_project(draft)
            kept = coordinator.conversation_work_items_by_file(
                later, "chess.py", include_kept_projects=True
            )
            assert [row["work_item_id"] for row in kept] == [draft]

            # The expensive rung is untouched: without the flag, nothing from
            # another conversation is visible, draft or not.
            assert coordinator.conversation_work_items_by_file(later, "emoji.py") == []
            assert coordinator.conversation_work_items_by_file(later, "chess.py") == []
            # And the conversation that did the work still sees both.
            assert len(
                coordinator.conversation_work_items_by_file("monday", "emoji.py")
            ) == 1
    print("ok: a kept place answers later; a draft stays inside its conversation")


def test_a_promoted_task_is_no_longer_reported_as_a_stranded_draft() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_promoted_wording_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, chess = _prepare(coordinator, task="Build a chess game")
                coordinator.promote_work_item_to_project(chess)
                # Telling the user to keep it as a project when they already
                # did would send them to a button that is no longer offered.
                assert coordinator.drafts_in_other_conversations("chess") == []
    print("ok: once kept as a project, a task stops being described as stranded")


def test_keeping_a_draft_takes_everything_that_happened_there() -> None:
    """Otherwise the project is missing the task that created it.

    Same-session iteration makes a new WorkItem in the same workspace by
    design, so a draft worked on three times is three tasks in one directory.
    Re-filing only the row the user clicked would leave the other two, and
    usually the original, outside the project they are plainly part of.
    """

    with tempfile.TemporaryDirectory(prefix="scratch_promote_siblings_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, first = _prepare(coordinator, task="Build a chess game")
                workspace = store.get_work_item(first).workspace_path
                _finish(store, first)
                # Iterating on it in the same session: a new task, same place.
                # The host hands back the earlier draft's directory, and that
                # directory is used rather than replaced by an empty one.
                _, second = _prepare(
                    coordinator, task="Add a timer to the chess game", cwd=Path(workspace)
                )
                _, elsewhere = _prepare(coordinator, task="Build a gomoku game")
                assert store.get_work_item(second).workspace_path == workspace

                # Working on a draft again must not quietly make it a project:
                # permanence is the thing someone has to decide to grant, and
                # granting it here would also withdraw the offer to grant it.
                assert (
                    store.get_work_item(second).project_id
                    == store.get_work_item(first).project_id
                )
                assert store.get_project_by_path(workspace) is None
                assert coordinator._project_item(store.get_work_item(second))[
                    "canPromoteToProject"
                ] is True
                assert coordinator.workspace_routing_context()["candidates"] == []

                promoted = coordinator.promote_work_item_to_project(second)
                assert promoted["refiledTasks"] == 2

                project_id = promoted["projectId"]
                assert store.get_work_item(first).project_id == project_id
                assert store.get_work_item(second).project_id == project_id
                # An unrelated draft is untouched; it is a different place.
                assert store.get_work_item(elsewhere).project_id != project_id
                assert {
                    row.work_item_id for row in store.list_work_items(project_id=project_id)
                } == {first, second}
    print("ok: keeping a draft takes every task that ran in that directory")


def test_retiring_a_project_takes_it_off_the_menu_and_nothing_else() -> None:
    """The only exit from a list that otherwise only grows.

    Keeping a place is a one-way ratchet without this, so something worth two
    days of attention would sit among the choices forever -- the same
    list-grows-with-history shape, at a slower rate.
    """

    with tempfile.TemporaryDirectory(prefix="scratch_retire_") as temp:
        root = Path(temp)
        project = root / "amadeus"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, task = _prepare(
                    coordinator,
                    task="Add emoji.py to the chat view",
                    cwd=project,
                    session_id="monday",
                )
                project_id = store.get_work_item(task).project_id
                _prepare(coordinator, task="Build a chess game", session_id="monday")
                # A draft never was a choice, so it cannot be retired from one.
                assert len(coordinator.workspace_routing_context()["candidates"]) == 1

                # Work still running is not a place to stop offering yet.
                try:
                    coordinator.set_project_retired(project_id, retired=True)
                    raise AssertionError("must refuse while work is running")
                except WorkLedgerConflict:
                    pass
                _finish(store, task)

                retired = coordinator.set_project_retired(project_id, retired=True)
                assert retired["state"] == "retired"
                assert coordinator.workspace_routing_context()["candidates"] == []

                # Hidden, not deleted: the files, the task and its past all
                # stay exactly as reachable as before.
                assert project.is_dir()
                assert store.get_work_item(task) is not None
                assert len(
                    coordinator.conversation_work_items_by_file(
                        "friday", "emoji.py", include_kept_projects=True
                    )
                ) == 1
                # Naming it outright still works; retiring stops it being
                # offered, it does not forbid it.
                assert coordinator.resolve_workspace_route(
                    {"project_id": project_id}
                )["status"] == "resolved"

                restored = coordinator.set_project_retired(project_id, retired=False)
                assert restored["state"] == "active"
                assert len(coordinator.workspace_routing_context()["candidates"]) == 1

                # The container is plumbing, not somewhere anyone chose to work.
                scratch_project = next(
                    row
                    for row in store.list_projects(include_retired=True)
                    if is_scratch_path(row.canonical_path)
                )
                try:
                    coordinator.set_project_retired(
                        scratch_project.project_id, retired=True
                    )
                    raise AssertionError("the scratch container is not retirable")
                except WorkLedgerConflict:
                    pass
    print("ok: retiring hides a project from the menu and changes nothing else")


def test_saying_which_project_once_routes_everything_after_it() -> None:
    """Asking per instruction does not work; saying it once does.

    Measured 2026-08-03: naming the project on every turn lands 2-4 times in
    12 and no wording moves it, because "this project" and a bare filename
    point at things the prompt does not contain. Said once it lands 6 in 6,
    and the working turns after it never repeat it -- so the host is the one
    that has to remember, which is what this covers.
    """

    with tempfile.TemporaryDirectory(prefix="scratch_session_project_") as temp:
        root = Path(temp)
        project = root / "amadeus"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, seed = _prepare(
                    coordinator,
                    task="修好聊天窗口的滚动条",
                    cwd=project,
                    session_id="monday",
                )
                project_id = store.get_work_item(seed).project_id

                # Before anyone says anything, an unnamed instruction is new
                # work -- that is what keeps one-offs out of the repository.
                before = coordinator.resolve_workspace_route({"session_id": "monday"})
                assert before["source"] == "scratch_default"

                coordinator.set_session_project("monday", project_id)
                after = coordinator.resolve_workspace_route({"session_id": "monday"})
                assert after["source"] == "session_project"
                assert Path(after["cwd"]) == project

                # It belongs to the conversation that said it, not the host.
                assert coordinator.resolve_workspace_route(
                    {"session_id": "friday"}
                )["source"] == "scratch_default"

                # Everything more specific still wins, so choosing a project
                # never overrides an instruction that named its own place.
                _, draft = _prepare(
                    coordinator, task="Build a chess game", session_id="tuesday"
                )
                draft_workspace = store.get_work_item(draft).workspace_path
                by_ref = coordinator.resolve_workspace_route(
                    {"session_id": "monday", "workspace_ref": draft}
                )
                assert Path(by_ref["cwd"]) == Path(draft_workspace)

                # And it can be moved, which is the second half of the workflow.
                kept = coordinator.promote_work_item_to_project(draft)
                coordinator.set_session_project("monday", kept["projectId"])
                moved = coordinator.resolve_workspace_route({"session_id": "monday"})
                assert Path(moved["cwd"]) == Path(draft_workspace)
    print("ok: a conversation's chosen project routes what follows, and can move")


def test_the_current_destination_is_visible() -> None:
    """The one way switching fails is a switch that was only spoken.

    Nothing else in the product would reveal that: the work simply lands in a
    fresh draft. So the destination has to be readable, and it has to say
    something true when no project was chosen rather than nothing at all.
    """

    with tempfile.TemporaryDirectory(prefix="scratch_destination_label_") as temp:
        root = Path(temp)
        project = root / "amadeus"
        project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, seed = _prepare(
                    coordinator, task="修好滚动条", cwd=project, session_id="monday"
                )
                project_id = store.get_work_item(seed).project_id
                with patch(
                    "core.session_manager.get_current_session_id",
                    return_value="monday",
                ):
                    assert coordinator.snapshot()["destinationLabel"] == ""
                    coordinator.set_session_project("monday", project_id)
                    assert coordinator.snapshot()["destinationLabel"] == "amadeus"

                    # A project that has gone from disk cannot be a destination,
                    # and must not keep being displayed as one.
                    coordinator.clear_session_project("monday")
                    assert coordinator.snapshot()["destinationLabel"] == ""
    print("ok: the surface can always say where the next instruction will go")


def test_a_row_says_what_kind_of_place_it_ran_in() -> None:
    """The workspace line is the only place a task says where it lives.

    Calling a draft's own repository the "main directory" invites the question
    "main directory of what?" -- there is no larger project it belongs to.
    """

    with tempfile.TemporaryDirectory(prefix="scratch_labels_") as temp:
        root = Path(temp)
        real_project = root / "amadeus"
        real_project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, project_task = _prepare(
                    coordinator, task="Fix the chat view", cwd=real_project
                )
                _, chess = _prepare(coordinator, task="Build a chess game")

                project_row = coordinator._project_item(store.get_work_item(project_task))
                assert project_row["workspaceLabel"].startswith("main directory · ")
                assert project_row["projectName"] == "amadeus"

                draft_row = coordinator._project_item(store.get_work_item(chess))
                assert draft_row["workspaceLabel"].startswith("draft · ")
                assert "main directory" not in draft_row["workspaceLabel"]
                # The container's ledger name is plumbing; it is not this task's
                # project and should not be shown as one.
                assert draft_row["projectName"] == ""

                coordinator.promote_work_item_to_project(chess)
                kept_row = coordinator._project_item(store.get_work_item(chess))
                assert kept_row["workspaceLabel"].startswith("draft · ")
                assert "· kept" in kept_row["workspaceLabel"]
                assert kept_row["projectName"] == "Build a chess game"
    print("ok: a row says whether it ran in a project, a draft, or a kept draft")


def test_finished_is_not_the_same_as_finished_with() -> None:
    with tempfile.TemporaryDirectory(prefix="scratch_reopen_") as temp:
        root = Path(temp)
        real_project = root / "amadeus"
        real_project.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store, _with_scratch_root(
            root / "scratch"
        ):
            coordinator = WorkLedgerCoordinator(store)
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                _, item_id = _prepare(
                    coordinator, task="Fix the chat view", cwd=real_project
                )
                # A task with a live attempt is never reopenable, whatever its
                # state -- reopening under a running provider would race it.
                store.set_work_item_state(item_id, "archived")
                assert coordinator._project_item(store.get_work_item(item_id))[
                    "canReopen"
                ] is False

                attempt = store.list_attempts(item_id)[-1]
                store.update_attempt(attempt.attempt_id, execution_status="succeeded")
                # An open task is not reopenable either; nothing to reopen.
                store.set_work_item_state(item_id, "open")
                assert coordinator._project_item(store.get_work_item(item_id))[
                    "canReopen"
                ] is False

                store.set_work_item_state(item_id, "archived")
                assert coordinator._project_item(store.get_work_item(item_id))[
                    "canReopen"
                ] is True

                # Reopening cannot work without the files, and the host must not
                # pretend otherwise -- the same rule the reopen path enforces.
                with patch.object(Path, "is_dir", return_value=False):
                    assert coordinator._project_item(store.get_work_item(item_id))[
                        "canReopen"
                    ] is False
    print("ok: an archived task offers reopen only while its workspace is there")


def test_slug_survives_titles_that_carry_no_latin_letters() -> None:
    assert slugify("Build a Chess Game!") == "build-a-chess-game"
    # Spoken Chinese/Japanese titles legitimately reduce to nothing; the caller's
    # id is what keeps the directory unique, so this must not raise or collide.
    assert slugify("做一个国际象棋游戏") == ""
    assert slugify("  ") == ""
    assert len(slugify("word " * 40)) <= 40
    print("ok: directory naming degrades quietly for titles with no ASCII")


def main() -> None:
    test_work_naming_no_project_never_reaches_a_real_repository()
    test_each_scratch_task_gets_its_own_repository()
    test_scratch_is_a_project_row_but_never_a_routing_candidate()
    test_a_draft_becomes_a_destination_only_when_the_user_says_so()
    test_exported_draft_promotion_materializes_editable_source_and_identity()
    test_legacy_generated_project_identity_can_be_repaired_without_overwrite()
    test_only_a_draft_can_be_promoted()
    test_scratch_failure_refuses_instead_of_choosing_a_real_directory()
    test_a_subdirectory_of_a_project_is_not_a_second_project()
    test_a_draft_is_findable_for_wording_but_never_for_routing()
    test_a_kept_place_stays_answerable_in_a_later_conversation()
    test_a_promoted_task_is_no_longer_reported_as_a_stranded_draft()
    test_keeping_a_draft_takes_everything_that_happened_there()
    test_retiring_a_project_takes_it_off_the_menu_and_nothing_else()
    test_saying_which_project_once_routes_everything_after_it()
    test_the_current_destination_is_visible()
    test_a_row_says_what_kind_of_place_it_ran_in()
    test_finished_is_not_the_same_as_finished_with()
    test_slug_survives_titles_that_carry_no_latin_letters()


if __name__ == "__main__":
    main()
