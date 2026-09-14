"""Resolve voice-level AUIP launch requests against verified Work artifacts.

Building an application, declaring AUIP capability, and entering an
AppSession are deliberately separate transitions:

* Provider Work creates or amends files.
* The artifact registry proves that a WorkItem currently contains one AUIP
  manifest and one unchanged entry document.
* This coordinator selects that application and asks a trusted renderer to
  open it.  Registration through the one-shot Attach ticket remains the first
  proof that an AppSession actually exists.

The role model may propose ``launch`` and a target token.  It never supplies a
path, AppSession id, or durable binding.  Ambiguity uses the existing Attention
primitive.  A same-turn "build it, then play" request is represented by one
expiring launch continuation keyed by the Chat turn, not by a permanent flag
on the WorkItem.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Protocol

from agent_host.work_ledger_store import WorkLedgerError
from server.reference_catalog import TypedReferenceCandidate

from server.attention_request import (
    AttentionOption,
    AttentionRequestCoordinator,
    opaque_option_id,
)
from server.auip_app_source import (
    ArtifactSource,
    discover_launchable_auip_app,
    list_current_runnable_artifacts,
)
from server.auip_contract import available_engagement_modes
from server.event_bus import bus
from server.protocol import Method
from server.work_permission_service import WorkPermissionService


logger = logging.getLogger(__name__)
LAUNCH_MODES = frozenset({"observe", "collaborate", "delegate"})
DEFERRED_LAUNCH_TTL_S = 30 * 60.0


class WorkRoster(Protocol):
    destination: Any

    def workspace_routing_context(self, *, limit: int = 8) -> dict[str, Any]: ...

    def project_apps(self, project_id: str, *, limit: int = 100) -> dict[str, Any]: ...

    def conversation_work_items_for_resolution(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AuipLaunchCandidate:
    token: str
    artifact_id: str
    artifact_ref: str
    app_id: str
    app_version: str
    work_item_id: str
    title: str
    work_title: str
    stances: tuple[str, ...]
    contributing_attempt_ids: tuple[str, ...]
    project_id: str = ""
    project_title: str = ""

    def prompt_dict(self) -> dict[str, Any]:
        return {
            "app": self.title,
            "work": self.work_title,
            "modes": available_engagement_modes(self.stances),
        }


@dataclass(frozen=True, slots=True)
class AuipPreparationCandidate:
    """Host-grounded Work that needs AUIP before it can launch.

    ``files`` is populated for a verified runnable delivery and remains empty
    while the same WorkItem is still producing its first Artifact.
    """

    work_item_id: str
    work_title: str
    files: tuple[str, ...]

    @property
    def title(self) -> str:
        return self.work_title


@dataclass(frozen=True, slots=True)
class _DeferredLaunch:
    session_id: str
    turn_id: str
    mode: str
    requested_at: float
    expires_at: float
    work_item_id: str = ""
    operation_id: str = ""
    input_id: str = ""
    source_app_session_id: str = ""


@dataclass(frozen=True, slots=True)
class _LaunchRequest:
    request_id: str
    session_id: str
    artifact_id: str
    created_at: float


Emitter = Callable[[str, dict[str, Any]], Awaitable[None]]
PreparationDispatcher = Callable[
    [AuipPreparationCandidate, str],
    Awaitable[dict[str, Any] | None],
]
BeforeResultEntry = Callable[[str, AuipLaunchCandidate], Awaitable[bool]]


class AuipLaunchCoordinator:
    """Host-owned selection and one-shot launch continuation."""

    def __init__(
        self,
        *,
        artifacts: ArtifactSource,
        work_roster: WorkRoster,
        attention: AttentionRequestCoordinator,
        emit: Emitter | None = None,
        clock: Callable[[], float] = time.time,
        before_result_entry: BeforeResultEntry | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.work_roster = work_roster
        self.attention = attention
        self.emit = emit or bus.emit
        self.clock = clock
        self.before_result_entry = before_result_entry
        self._deferred: dict[tuple[str, str], _DeferredLaunch] = {}
        self._launch_requests: dict[str, _LaunchRequest] = {}

    def _complete_roster_rows(self, session_id: str) -> list[dict[str, Any]] | None:
        roster = self.work_roster.conversation_work_items_for_resolution(
            str(session_id or ""),
            limit=200,
        )
        if not isinstance(roster, dict) or roster.get("complete") is not True:
            logger.warning(
                "AUIP candidate resolution refused an incomplete Work roster "
                "session_id=%s",
                session_id,
            )
            return None
        rows = roster.get("items")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            logger.warning(
                "AUIP candidate resolution refused a malformed Work roster "
                "session_id=%s",
                session_id,
            )
            return None
        return rows

    def candidates(self, session_id: str, *, limit: int = 8) -> list[AuipLaunchCandidate]:
        return self.entry_candidates(session_id, limit=limit)[0]

    def entry_candidates(self, session_id: str, *, limit: int = 8
            ) -> tuple[list[AuipLaunchCandidate], list[AuipPreparationCandidate]]:
        """Capture launch and preparation capabilities in one discovery."""
        return self._entry_candidates(session_id, limit=limit)

    def preparation_candidates(
        self,
        session_id: str,
        *,
        limit: int = 8,
    ) -> list[AuipPreparationCandidate]:
        """List runnable WorkItems that are not yet launchable AUIP apps.

        These rows are capability facts for the source-local AUIP decision.
        They are deliberately absent from the speaking role prompt: exposing
        more Work inventory there did not repair action omission and taxed
        every ordinary response.
        """

        return self.entry_candidates(session_id, limit=limit)[1]

    def has_project_history(self) -> bool:
        return bool(self.work_roster.workspace_routing_context(limit=1).get("candidateCount"))

    def project_references(self, session_id: str) -> tuple[tuple[TypedReferenceCandidate, ...], bool]:
        routing = self.work_roster.workspace_routing_context(limit=200)
        current = self.work_roster.destination.session_project(session_id)
        return tuple(TypedReferenceCandidate(kind="project", entity_id=row["projectId"],
            label=row["projectName"], scope="persistent", aliases=tuple(row.get("projectAliases") or ()),
            session_current=row["projectId"] == current)
            for row in routing.get("candidates") or []), routing.get("candidatesComplete") is True

    def project_candidates(self, session_id: str, *, current_only: bool,
            project_ids: tuple[str, ...] | None = None) -> tuple[tuple[AuipLaunchCandidate, ...], bool]:
        """Read the existing durable Project app indexes only on a cold lookup."""
        routing = self.work_roster.workspace_routing_context(limit=200)
        if routing.get("candidatesComplete") is not True:
            return (), False
        current = self.work_roster.destination.session_project(session_id)
        projects = [row for row in routing.get("candidates") or []
            if (row.get("projectId") in project_ids if project_ids is not None
                else (row.get("projectId") == current) == current_only)]
        if project_ids is not None and {row["projectId"] for row in projects} != set(project_ids):
            return (), False
        candidates = []
        for project in projects:
            catalog = self.work_roster.project_apps(project["projectId"], limit=200)
            if catalog.get("complete") is not True:
                return (), False
            for app in catalog.get("apps") or []:
                candidates.append(AuipLaunchCandidate(
                    token="auip:" + app["artifactId"], artifact_id=app["artifactId"],
                    artifact_ref=app["artifactRef"], app_id=app["appId"], app_version=app["version"],
                    work_item_id=app["workItemId"], title=app["title"], work_title=app["workTitle"],
                    stances=("spectator", "participant") if "collaborate" in app["modes"] else ("spectator",),
                    contributing_attempt_ids=(), project_id=project["projectId"],
                    project_title=project["projectName"]))
        return tuple(candidates), True

    def entry_references(self, candidates) -> tuple[tuple[TypedReferenceCandidate, ...], bool]:
        """Describe the exact Work/Project owners of one frozen app set."""
        references = []
        for index, candidate in enumerate(candidates):
            item = self.artifacts.get_work_item(candidate.work_item_id)
            if item is None:
                return (), False
            draft = self.work_roster.destination.is_unkept_draft(item.workspace_path)
            project = self.artifacts.get_project(item.project_id)
            if not draft and project is None:
                return (), False
            references.append(TypedReferenceCandidate(kind="work_item",
                entity_id=item.work_item_id, label=candidate.title,
                scope="session_draft" if draft else "project",
                parent_project_id="" if draft else project.project_id,
                parent_project_label="" if draft else project.name,
                aliases=(candidate.work_title,), recency_rank=index,
                delegated_goal=item.goal))
        return tuple(references), True

    def _entry_candidates(
        self, session_id: str, *, limit: int,
    ) -> tuple[list[AuipLaunchCandidate], list[AuipPreparationCandidate]]:
        """Both entry kinds share one completeness boundary per discovery.

        An overflow in either kind invalidates the whole view. Otherwise an
        unrepresented kind could make the remaining kind appear uniquely bound.
        Preserve the existing per-kind bound; do not truncate either list.
        """
        rows = self._complete_roster_rows(session_id)
        if rows is None:
            return [], []
        recent_drafts = None
        draft_reader = getattr(self.work_roster, "draft_apps", None)
        draft_check = getattr(getattr(self.work_roster, "destination", None), "is_unkept_draft", None)
        if callable(draft_reader):
            # Share the visible Draft shelf across conversations. Keeping one
            # active Work pointer is not an application-history index.
            shelf = draft_reader(limit=5)
            apps = list(shelf.get("apps") or [])
            if shelf.get("complete") is not True and len(apps) < 5:
                return [], []
            recent_drafts = {str(app["workItemId"]) for app in apps}
            seen = {str(row.get("work_item_id") or "") for row in rows}
            rows = [*rows, *({"work_item_id":app["workItemId"], "title":app["workTitle"]}
                for app in apps if str(app["workItemId"]) not in seen)]
        candidate_limit = max(1, min(int(limit), 8))
        launch: list[AuipLaunchCandidate] = []
        prepare: list[AuipPreparationCandidate] = []
        for row in rows:
            work_item_id = str(row.get("work_item_id") or "").strip()
            if not work_item_id:
                continue
            app = discover_launchable_auip_app(self.artifacts, work_item_id)
            if app is not None:
                project_title = ""
                if recent_drafts is not None and callable(draft_check):
                    item = self.artifacts.get_work_item(work_item_id)
                    if item is not None:
                        if draft_check(item.workspace_path):
                            if work_item_id not in recent_drafts:
                                continue
                        else:
                            project = self.artifacts.get_project(item.project_id)
                            project_title = str(project.name if project is not None else "")
                app_meta = app.get("app") if isinstance(app.get("app"), dict) else {}
                artifact_id = str(app.get("artifact_id") or "")
                launch.append(
                    AuipLaunchCandidate(
                        token=f"auip:{artifact_id}",
                        artifact_id=artifact_id,
                        artifact_ref=str(app.get("artifact_ref") or ""),
                        app_id=str(app_meta.get("id") or artifact_id),
                        app_version=str(app_meta.get("version") or "0"),
                        work_item_id=work_item_id,
                        title=str(app_meta.get("title") or app.get("title") or "AUIP app"),
                        work_title=str(row.get("title") or "WorkItem"),
                        stances=tuple(str(value) for value in app.get("stances") or []),
                        contributing_attempt_ids=tuple(
                            str(value) for value in app.get("contributing_attempt_ids") or []
                        ),
                        project_title=project_title,
                    )
                )
            else:
                runnable = list_current_runnable_artifacts(self.artifacts, work_item_id)
                if not runnable:
                    continue
                file_names = tuple(
                    dict.fromkeys(
                        str(getattr(record, "path", "")).replace("\\", "/").rsplit("/", 1)[-1]
                        for record in runnable
                        if str(getattr(record, "path", "")).strip()
                    )
                )
                prepare.append(
                    AuipPreparationCandidate(
                        work_item_id=work_item_id,
                        work_title=str(row.get("title") or "WorkItem"),
                        files=file_names[:3],
                    )
                )
            if len(launch) > candidate_limit or len(prepare) > candidate_limit:
                logger.warning(
                    "AUIP entry candidate resolution exceeded its complete per-kind "
                    "bound session_id=%s limit=%d",
                    session_id,
                    candidate_limit,
                )
                return [], []
        return launch, prepare

    def render_prompt_context(
        self,
        session_id: str,
        *,
        language: str = "en",
        include_control_contract: bool = True,
    ) -> str:
        candidates = self.candidates(session_id)
        japanese = str(language or "").lower().startswith("ja")
        capability_boundary = (
            "確認済みの対話型アプリでは観戦・共同参加・委任参加ができる。候補が none でも、"
            "それは現在この会話で起動可能なアプリを確認できていないという意味であり、"
            "『私はアプリを操作できない』という恒久的な能力否定に言い換えない。"
            if japanese
            else "Amadeus can observe, collaborate in, or take delegated participation "
            "in verified interactive applications. If the candidate list is none, say "
            "only that no launchable application is currently verified for this "
            "conversation; never turn that scoped fact into a permanent claim that you "
            "cannot operate applications."
        )
        roster = "\n".join(
            f"- app={_inline(item.title)}; "
            f"work={_inline(item.work_title)}; modes={','.join(item.prompt_dict()['modes'])}"
            for item in candidates
        ) or "- none"
        if not include_control_contract:
            rules = (
                [
                    "[AUIP launchable applications]",
                    "アプリの作成・変更は Provider Work であり、それだけでアプリを開いたことにはならない。",
                    capability_boundary,
                    "launchable_apps は起動可能な候補であって、現在開いている証明ではない。[Current AUIP app experience] がない限り、過去の会話に「開いた」とあっても、すでに開いていると言わない。",
                    "既存の launchable app を開く・始める・遊ぶだけの要求は Host の AUIP control が担当する。その体験遷移を実現するための Provider Work は別に提案しないが、共通の制御結果形式は常に守る。同じ発話に coding、research、external action、別の delivery が独立して含まれるなら、その部分には通常の Provider Work control を使う。",
                    "すでに実行中の Work が終わった後に開くよう求められただけなら、その Work を再委託しない。完了や起動を先取りせず、予定として自然に応答する。",
                    "依頼には自然に応じるが、Host が AppSession の接続または失敗を確認する前に『開いた』『開始した』と断言しない。",
                    "launchable_apps:",
                    roster,
                    "[/AUIP launchable applications]",
                ]
                if str(language or "").lower().startswith("ja")
                else [
                    "[AUIP launchable applications]",
                    "Creating or changing an app is Provider Work; that alone does not mean it opened.",
                    capability_boundary,
                    "launchable_apps are candidates the Host can start, not proof that they are currently open. Unless a [Current AUIP app experience] block exists, never say an app is already open merely because earlier conversation claimed it was.",
                    "A request only to open, start, or play an existing launchable app is owned by Host AUIP control. Do not propose separate Provider Work merely to perform that experience transition, but always obey the shared control-outcome format. If the same utterance independently asks for coding, research, an external action, or another delivery, keep ordinary Provider Work control for that separate clause.",
                    "If the user only asks to open it after already-active Work finishes, do not delegate that Work again. Acknowledge the plan without claiming either completion or launch.",
                    "Respond naturally to the request, but do not claim the app opened or started until the Host reports an AppSession connection or a launch failure.",
                    "launchable_apps:",
                    roster,
                    "[/AUIP launchable applications]",
                ]
            )
        elif str(language or "").lower().startswith("ja"):
            rules = [
                "[AUIP launch control]",
                "アプリの作成・変更は Provider Work であり、それだけでアプリを開いてはいけない。",
                capability_boundary,
                "launchable_apps は起動可能な候補であって、現在開いている証明ではない。[Current AUIP app experience] がない限り、過去の会話に「開いた」とあっても、すでに開いていると言わない。",
                "ユーザーが明示的に開く・始める・一緒に遊ぶよう求めた時だけ、[AUIP action=launch target=\"表示された app 名\" mode=\"observe|collaborate|delegate\"] を一つ出す。候補が一つなら target は省略できる。内部 ID は推測も転記もしない。",
                "同じ発話で作成/変更の完了後すぐ体験へ入ることも明示された場合だけ、DELEGATE/CONTROL より前に [AUIP action=launch target=\"delivery\" mode=\"...\" after=\"work\"] を出す。この予約は本 turn の Work に一度だけ結び付く。",
                "AUIP タグは要求の提案であり、起動成功の証明ではない。最初の返答は依頼を受けた言い方に留め、Host が AppSession の接続または失敗を確認する前に『開いた』『開始した』と完了形で断言しない。",
                "候補が複数で対象が不明なら推測せず、短く選択を案内する。launch は WorkItem の永続状態ではない。",
                "launchable_apps:",
                roster,
                "[/AUIP launch control]",
            ]
        else:
            rules = [
                "[AUIP launch control]",
                "Creating or changing an app is Provider Work; that alone never opens it.",
                capability_boundary,
                "launchable_apps are candidates the Host can start, not proof that they are currently open. Unless a [Current AUIP app experience] block exists, never say an app is already open merely because earlier conversation claimed it was.",
                "Only when the user explicitly asks to open, start, or play it, emit one [AUIP action=launch target=\"displayed app name\" mode=\"observe|collaborate|delegate\"]. Omit target when there is one candidate. Never invent or copy an internal id.",
                "Only when the same utterance explicitly asks to enter the experience after build/amend completes, emit [AUIP action=launch target=\"delivery\" mode=\"...\" after=\"work\"] before DELEGATE/CONTROL. This reserves one launch for this turn's Work only.",
                "The tag proposes the requested transition; it is not proof that launch succeeded. Acknowledge the request without claiming the app opened or started until the Host reports an AppSession connection or a launch failure.",
                "If several candidates fit and the target is unclear, do not guess; briefly tell the user to choose. Launch is not durable WorkItem state.",
                "launchable_apps:",
                roster,
                "[/AUIP launch control]",
            ]
        return "\n".join(rules)

    async def route_control(
        self,
        attrs: dict[str, Any],
        *,
        session_id: str,
        turn_id: str,
        prepare_work: PreparationDispatcher | None = None,
        source_app_session_id: str = "",
    ) -> dict[str, Any]:
        frozen = attrs.get("_host_launch_candidates")
        if frozen is not None:
            # Only Host candidate objects can cross this seam. Model JSON
            # cannot turn an id/name into a captured artifact identity.
            if (attrs.get("action") != "launch" or attrs.get("after")
                    or not isinstance(frozen, tuple) or not frozen
                    or any(not isinstance(item, AuipLaunchCandidate) for item in frozen)):
                return {"ok": False, "error": "invalid_launch_binding"}
            if len(frozen) == 1:
                return await self._emit_launch(session_id, frozen[0], _mode(attrs.get("mode")))
            return await self._request_selection(session_id, list(frozen), _mode(attrs.get("mode")))
        if str(attrs.get("action") or "").strip().lower() == "prepare":
            return await self._route_preparation(
                attrs,
                session_id=session_id,
                turn_id=turn_id,
                prepare_work=prepare_work,
            )
        mode = _mode(attrs.get("mode"))
        target = str(attrs.get("target") or "").strip()
        after = str(attrs.get("after") or "").strip().lower()
        delivery_target = target.lower() == "delivery"
        deferred_timing = after == "work"
        if delivery_target != deferred_timing:
            await self._announce_failure(session_id, "invalid_launch_timing")
            return {"ok": False, "error": "invalid_launch_timing"}
        if delivery_target:
            source_app_session_id = str(source_app_session_id or "").strip()
            clean_turn = str(turn_id or "").strip()
            if not clean_turn:
                await self._announce_failure(session_id, "launch_turn_unavailable")
                return {"ok": False, "error": "launch_turn_unavailable"}
            binding = str(attrs.get("_host_work_binding") or "").strip().lower()
            raw_input_id = attrs.get("_host_work_input_id")
            if raw_input_id is None:
                work_input_id = ""
            elif (not isinstance(raw_input_id, str) or not raw_input_id
                    or raw_input_id != raw_input_id.strip()):
                await self._announce_failure(session_id, "invalid_work_input_binding")
                return {"ok": False, "error": "invalid_work_input_binding"}
            else:
                work_input_id = raw_input_id
            if work_input_id and binding != "active":
                await self._announce_failure(session_id, "invalid_work_input_binding")
                return {"ok": False, "error": "invalid_work_input_binding"}
            if binding == "active":
                attempt_ids = tuple(
                    dict.fromkeys(
                        str(value).strip()
                        for value in (
                            attrs.get("_host_active_work_attempt_ids") or ()
                        )
                        if str(value).strip()
                    )
                )
                rows = self._active_work_rows(session_id, attempt_ids)
                if not rows:
                    await self._announce_failure(
                        session_id,
                        "deferred_work_unavailable",
                    )
                    return {"ok": False, "error": "deferred_work_unavailable"}
                if len(rows) > 1:
                    return await self._request_deferred_work_selection(
                        session_id,
                        clean_turn,
                        rows,
                        mode,
                        work_input_id,
                        source_app_session_id,
                    )
                return await self._bind_deferred_work(
                    session_id,
                    clean_turn,
                    rows[0],
                    mode,
                    work_input_id,
                    source_app_session_id,
                )
            if binding != "turn":
                await self._announce_failure(session_id, "invalid_work_binding")
                return {"ok": False, "error": "invalid_work_binding"}
            now = float(self.clock())
            pending = _DeferredLaunch(
                session_id=str(session_id or ""),
                turn_id=clean_turn,
                mode=mode,
                requested_at=now,
                expires_at=now + DEFERRED_LAUNCH_TTL_S,
                work_item_id=str(attrs.get("_host_work_item_id") or "").strip(),
                source_app_session_id=source_app_session_id,
            )
            self._deferred[(pending.session_id, pending.turn_id)] = pending
            return {"ok": True, "deferred": True, "turn_id": pending.turn_id}

        candidates = self.candidates(session_id)
        matches = _matches(candidates, target)
        if not target and len(candidates) == 1:
            matches = candidates
        if len(matches) == 1:
            return await self._emit_launch(session_id, matches[0], mode)
        if not candidates:
            await self._announce_failure(session_id, "no_launchable_auip_app")
            return {"ok": False, "error": "no_launchable_auip_app"}
        if target and not matches:
            await self._announce_failure(session_id, "launch_target_not_found")
            return {"ok": False, "error": "launch_target_not_found"}
        ambiguous = matches if len(matches) > 1 else candidates
        return await self._request_selection(session_id, ambiguous, mode)

    async def _route_preparation(
        self,
        attrs: dict[str, Any],
        *,
        session_id: str,
        turn_id: str,
        prepare_work: PreparationDispatcher | None,
    ) -> dict[str, Any]:
        """Resolve one runnable or uniquely active Work before ordinary amend."""

        if prepare_work is None:
            await self._announce_failure(session_id, "preparation_unavailable")
            return {"ok": False, "error": "preparation_unavailable"}
        active_attempt_ids = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in (attrs.get("_host_active_work_attempt_ids") or ())
                if str(value).strip()
            )
        )
        if active_attempt_ids:
            rows = self._active_work_rows(session_id, active_attempt_ids)
            candidates = [
                AuipPreparationCandidate(
                    work_item_id=str(row.get("work_item_id") or ""),
                    work_title=str(row.get("title") or "WorkItem"),
                    files=(),
                )
                for row in rows
                if str(row.get("work_item_id") or "").strip()
            ]
            candidates = list(
                {
                    candidate.work_item_id: candidate
                    for candidate in candidates
                }.values()
            )
            if len(candidates) == 1:
                return await self._begin_preparation(
                    session_id,
                    turn_id,
                    candidates[0],
                    _mode(attrs.get("mode")),
                    prepare_work,
                )
            if len(candidates) > 1:
                return await self._request_preparation_selection(
                    session_id,
                    turn_id,
                    candidates,
                    _mode(attrs.get("mode")),
                    prepare_work,
                )
            await self._announce_failure(session_id, "deferred_work_unavailable")
            return {"ok": False, "error": "deferred_work_unavailable"}
        candidates = self.preparation_candidates(session_id)
        frozen_work_item_id = str(
            attrs.get("_host_preparation_work_item_id") or ""
        ).strip()
        target = str(attrs.get("target") or "").strip()
        matches = [
            item
            for item in candidates
            if (
                frozen_work_item_id
                and item.work_item_id == frozen_work_item_id
            )
            or (
                not frozen_work_item_id
                and target
                and item.title.casefold() == target.casefold()
            )
        ]
        if not frozen_work_item_id and not target and len(candidates) == 1:
            matches = candidates
        if len(matches) == 1:
            return await self._begin_preparation(
                session_id,
                turn_id,
                matches[0],
                _mode(attrs.get("mode")),
                prepare_work,
            )
        if not candidates:
            await self._announce_failure(session_id, "no_preparable_auip_app")
            return {"ok": False, "error": "no_preparable_auip_app"}
        if (target or frozen_work_item_id) and not matches:
            await self._announce_failure(session_id, "preparation_target_not_found")
            return {"ok": False, "error": "preparation_target_not_found"}
        ambiguous = matches if len(matches) > 1 else candidates
        return await self._request_preparation_selection(
            session_id,
            turn_id,
            ambiguous,
            _mode(attrs.get("mode")),
            prepare_work,
        )

    async def _begin_preparation(
        self,
        session_id: str,
        turn_id: str,
        candidate: AuipPreparationCandidate,
        mode: str,
        prepare_work: PreparationDispatcher,
    ) -> dict[str, Any]:
        clean_session = str(session_id or "").strip()
        clean_turn = str(turn_id or "").strip()
        if not clean_session or not clean_turn:
            await self._announce_failure(session_id, "preparation_turn_unavailable")
            return {"ok": False, "error": "preparation_turn_unavailable"}
        now = float(self.clock())
        key = (clean_session, clean_turn)
        self._deferred[key] = _DeferredLaunch(
            session_id=clean_session,
            turn_id=clean_turn,
            mode=mode,
            requested_at=now,
            expires_at=now + DEFERRED_LAUNCH_TTL_S,
            work_item_id=candidate.work_item_id,
        )
        try:
            work = await prepare_work(candidate, mode)
        except Exception:
            logger.exception(
                "[AUIP-LAUNCH] preparation dispatch failed session=%s turn=%s",
                clean_session,
                clean_turn,
            )
            return {"ok": False, "uncertain": True, "error": "preparation_handoff_unknown"}
        if isinstance(work, dict):
            if work.get("state") != "work_started":
                rejected = work.get("state") == "rejected"
                if rejected:
                    self._deferred.pop(key, None)
                return {"ok":False, "uncertain":not rejected,
                    "error":str(work.get("reason") or "preparation_not_started"), "work":work}
            attempt = self.artifacts.get_attempt(str(work.get("attempt_id") or ""))
            pending = self._deferred.get(key)
            if attempt is not None and pending is not None:
                self._deferred[key] = replace(pending, operation_id=attempt.operation_id)
        return {
            "ok": True,
            "deferred": True,
            "preparing": True,
            "turn_id": clean_turn,
            **({"work":work} if isinstance(work, dict) else {}),
        }

    async def _request_preparation_selection(
        self,
        session_id: str,
        turn_id: str,
        candidates: list[AuipPreparationCandidate],
        mode: str,
        prepare_work: PreparationDispatcher,
    ) -> dict[str, Any]:
        by_option: dict[str, AuipPreparationCandidate] = {}
        options: list[AttentionOption] = []
        for candidate in candidates:
            option_id = opaque_option_id()
            by_option[option_id] = candidate
            options.append(
                AttentionOption(
                    option_id=option_id,
                    label=candidate.title,
                    entity_kind="work_item",
                    description="Prepare this application for an AUIP experience",
                    metadata={"scope": "auip_prepare", "relation": "experience"},
                )
            )

        async def resume(option_id: str) -> dict[str, Any]:
            return await self._begin_preparation(
                session_id,
                turn_id,
                by_option[option_id],
                mode,
                prepare_work,
            )

        request = await self.attention.create_selection(
            session_id=session_id,
            title="Choose an application",
            prompt="Which existing application should be prepared and opened?",
            options=options,
            continuation=resume,
            dedupe_key="auip.prepare",
        )
        return {"ok": True, "deferred": True, "attention": request}

    def _deferred_input_state(
        self,
        pending: _DeferredLaunch,
        matching_attempts: list[Any],
    ) -> tuple[str, str]:
        """Re-read one optional input receipt from its Work/Operation owner."""

        if not pending.input_id:
            return "not_required", ""
        list_inputs = getattr(self.artifacts, "list_provider_inputs", None)
        if not callable(list_inputs):
            return "waiting", ""
        try:
            matches = [
                row
                for row in list_inputs(pending.work_item_id)
                if isinstance(row, dict) and row.get("input_id") == pending.input_id
            ]
        except (WorkLedgerError, OSError, TypeError, ValueError):
            return "waiting", ""
        if len(matches) != 1:
            return "waiting", ""
        receipt = matches[0]
        if not any(
            str(getattr(attempt, "attempt_id", ""))
            == str(receipt.get("attempt_id") or "")
            and str(getattr(attempt, "provider_run_id", ""))
            == str(receipt.get("provider_run_id") or "")
            for attempt in matching_attempts
        ):
            return "waiting", ""
        state = str(receipt.get("state") or "")
        return (
            state if state in {"delivered", "rejected"} else "waiting",
            str(receipt.get("attempt_id") or ""),
        )

    async def _result_entry_ready(
        self,
        key: tuple[str, str],
        pending: _DeferredLaunch,
        candidate: AuipLaunchCandidate,
    ) -> bool:
        source_app_session_id = pending.source_app_session_id
        if not source_app_session_id:
            return True
        callback = self.before_result_entry
        if callback is None:
            if self._deferred.get(key) is pending:
                self._deferred.pop(key, None)
                await self._announce_failure(
                    pending.session_id,
                    "result_entry_unavailable",
                )
            return False
        try:
            ready = await callback(source_app_session_id, candidate)
        except Exception:
            if self._deferred.get(key) is not pending:
                return False
            self._deferred.pop(key, None)
            logger.exception(
                "[AUIP-LAUNCH] result entry failed session=%s turn=%s source=%s",
                pending.session_id,
                pending.turn_id,
                source_app_session_id,
            )
            await self._announce_failure(
                pending.session_id,
                "result_entry_failed",
            )
            return False
        return ready is True

    async def on_app_updated(self, _method: str, payload: dict[str, Any]) -> None:
        if not self._deferred or not isinstance(payload, dict):
            return
        source_app_session_id = str(payload.get("app_session_id") or "").strip()
        if not source_app_session_id:
            return
        status = str(payload.get("status") or "").strip().lower()
        surface_close_status = str(
            payload.get("surface_close_status") or ""
        ).strip().lower()
        if (
            status not in {"closed", "disconnected"}
            and surface_close_status not in {"closed", "failed"}
        ):
            return
        if not any(
            pending.source_app_session_id == source_app_session_id
            for pending in self._deferred.values()
        ):
            return
        await self.on_work_updated(
            _method,
            payload,
            source_app_session_id=source_app_session_id,
        )

    async def on_work_updated(
        self,
        _method: str,
        payload: dict[str, Any],
        *,
        source_app_session_id: str = "",
        pending_key: tuple[str, str] | None = None,
    ) -> None:
        if not self._deferred:
            return
        now = float(self.clock())
        for key, pending in list(self._deferred.items()):
            if pending_key is not None and key != pending_key:
                continue
            if (
                source_app_session_id
                and pending.source_app_session_id != source_app_session_id
            ):
                continue
            if pending.expires_at <= now:
                self._deferred.pop(key, None)
                logger.warning(
                    "[AUIP-LAUNCH] deferred continuation expired session=%s "
                    "turn=%s work_item=%s elapsed_s=%.1f",
                    pending.session_id,
                    pending.turn_id,
                    pending.work_item_id,
                    max(0.0, now - pending.requested_at),
                )
                await self._announce_failure(
                    pending.session_id,
                    "deferred_launch_expired",
                    detail=(
                        "AUIP launch continuation expired before a verified "
                        "application became available"
                    ),
                )
                continue
            if pending.work_item_id and pending.operation_id:
                matching_attempts = self._attempts_for_operation(
                    pending.work_item_id,
                    pending.operation_id,
                )
            else:
                matching_attempts = self._attempts_for_turn(
                    pending.session_id,
                    pending.turn_id,
                )
                if pending.work_item_id:
                    matching_attempts = [
                        attempt
                        for attempt in matching_attempts
                        if str(getattr(attempt, "work_item_id", ""))
                        == pending.work_item_id
                    ]
            input_state, input_attempt_id = self._deferred_input_state(
                pending, matching_attempts
            )
            if input_state == "rejected":
                self._deferred.pop(key, None)
                continue
            if input_state == "waiting":
                continue
            if any(
                str(getattr(attempt, "execution_status", ""))
                in {"queued", "running", "orphaned"}
                for attempt in matching_attempts
            ):
                continue
            terminal = [
                attempt
                for attempt in matching_attempts
                if str(getattr(attempt, "execution_status", ""))
                in {"succeeded", "failed", "cancelled"}
            ]
            if not terminal:
                continue
            input_result_attempt_ids = {input_attempt_id} if input_attempt_id else set()
            predecessor_for = getattr(self.artifacts, "get_recovery_predecessor", None)
            if input_attempt_id and callable(predecessor_for):
                for attempt in terminal:
                    predecessor = predecessor_for(attempt)
                    if predecessor is not None and predecessor.attempt_id == input_attempt_id:
                        input_result_attempt_ids.add(attempt.attempt_id)
            succeeded_ids = {
                str(attempt.attempt_id)
                for attempt in terminal
                if str(attempt.execution_status) == "succeeded"
                and (not input_attempt_id
                    or str(attempt.attempt_id) in input_result_attempt_ids)
            }
            candidate = next(
                (
                    item
                    for item in self.candidates(pending.session_id)
                    if self._candidate_matches_attempts(item, succeeded_ids)
                    and _candidate_supports_mode(item, pending.mode)
                ),
                None,
            )
            relevant_attempt_ids = (
                set(candidate.contributing_attempt_ids)
                if candidate is not None
                else succeeded_ids
            )
            delivery_permissions = self._desktop_export_permissions(
                {
                    str(getattr(attempt, "work_item_id", ""))
                    for attempt in terminal
                    if str(getattr(attempt, "attempt_id", ""))
                    in relevant_attempt_ids
                },
                relevant_attempt_ids,
            )
            relevant_attempts = [
                attempt
                for attempt in terminal
                if str(getattr(attempt, "attempt_id", ""))
                in relevant_attempt_ids
            ]
            if candidate is None and any(
                self._attempt_has_rejected_auip_outcome(attempt)
                for attempt in relevant_attempts
            ):
                # A completed Host verdict is no longer an artifact/export
                # race. Rejected delivery will not create export permission;
                # the Work terminal already owns its user-visible failure.
                self._deferred.pop(key, None)
                continue
            requires_desktop_delivery = any(
                self._attempt_requires_desktop_delivery(attempt)
                for attempt in relevant_attempts
            )
            if requires_desktop_delivery and (
                not delivery_permissions
                or any(
                    permission.status == "pending"
                    for permission in delivery_permissions
                )
                or (
                    any(
                        permission.status == "allowed"
                        for permission in delivery_permissions
                    )
                    and candidate is None
                )
            ):
                # Provider success only proves the staged bytes.  Keep the
                # one-shot continuation alive until the user's delivery
                # transaction either materializes an approved revision or is
                # declined.  Permission resolution publishes another Work
                # update, which re-enters this same state evaluation.
                continue
            if requires_desktop_delivery and not any(
                permission.status == "allowed"
                for permission in delivery_permissions
            ):
                # A denied or expired delivery transaction is the terminal
                # authority fact.  Never fall back to opening its transaction
                # staging or another workspace copy behind that decision.
                self._deferred.pop(key, None)
                continue
            if candidate is None:
                if succeeded_ids:
                    # Attempt terminal can be projected before its immutable
                    # artifact rows are reconciled.  Keep the one-shot launch
                    # alive so the following Work update can discover the
                    # exact contributing bundle.  A failed/cancelled Attempt
                    # has no such pending success evidence and settles below.
                    continue
                # The preparation Attempt has already reached the Work Ledger
                # terminal boundary. Its Observer report owns the failure and
                # explains why no launch followed; emitting a second synthetic
                # AUIP terminal here makes one user action speak twice.
                self._deferred.pop(key, None)
                continue
            if not await self._result_entry_ready(key, pending, candidate):
                continue
            # AUIP_UPDATED can re-enter during the readiness callback. Only
            # the exact pending continuation may consume this launch.
            if self._deferred.get(key) is not pending:
                continue
            self._deferred.pop(key, None)
            await self._emit_launch(pending.session_id, candidate, pending.mode)

    def _desktop_export_permissions(
        self,
        work_item_ids: set[str],
        attempt_ids: set[str],
    ) -> list[Any]:
        """Read only delivery authority belonging to the causal Attempts."""

        list_permissions = getattr(self.artifacts, "list_permission_requests", None)
        if not callable(list_permissions):
            return []
        permissions: list[Any] = []
        for work_item_id in sorted(work_item_ids - {""}):
            permissions.extend(
                permission
                for permission in list_permissions(work_item_id)
                if str(getattr(permission, "attempt_id", "")) in attempt_ids
                and WorkPermissionService.is_desktop_export_permission(permission)
            )
        return permissions

    @staticmethod
    def _attempt_requires_desktop_delivery(attempt: Any) -> bool:
        metadata = (
            getattr(attempt, "metadata", {})
            if isinstance(getattr(attempt, "metadata", {}), dict)
            else {}
        )
        plan = (
            metadata.get("export_plan")
            if isinstance(metadata.get("export_plan"), dict)
            else {}
        )
        return str(plan.get("kind") or "").strip().lower() == "desktop"

    def _candidate_matches_attempts(self, candidate, attempt_ids: set[str]) -> bool:
        """The newest contributing revision must belong to the awaited work."""
        contributors = [self.artifacts.get_attempt(attempt_id)
            for attempt_id in candidate.contributing_attempt_ids]
        if not contributors or any(attempt is None
                or attempt.work_item_id != candidate.work_item_id for attempt in contributors):
            return False
        newest = max(contributors, key=lambda attempt:attempt.attempt_number)
        return newest.attempt_id in attempt_ids

    @staticmethod
    def _attempt_has_rejected_auip_outcome(attempt: Any) -> bool:
        metadata = (
            getattr(attempt, "metadata", {})
            if isinstance(getattr(attempt, "metadata", {}), dict)
            else {}
        )
        verdict = (
            metadata.get("outcome_verdict")
            if isinstance(metadata.get("outcome_verdict"), dict)
            else {}
        )
        validation = metadata.get("host_auip_bundle_validation")
        if (isinstance(validation, dict)
                and validation.get("recovery_state") == "cancelled"):
            return True
        return bool(
            str(verdict.get("facet") or "").strip().lower()
            == "auip.application"
            and verdict.get("verified") is False
        )

    async def record_client_result(
        self,
        *,
        session_id: str,
        request_id: str,
        status: str,
        detail: str = "",
    ) -> dict[str, Any]:
        clean_request_id = str(request_id or "")
        request = self._launch_requests.get(clean_request_id)
        if request is None:
            return {"ok": False, "error": "launch_request_not_found"}
        if request.session_id != str(session_id or ""):
            return {"ok": False, "error": "launch_session_mismatch"}
        self._launch_requests.pop(clean_request_id, None)
        clean_status = str(status or "").strip().lower()
        if clean_status != "opened":
            logging.getLogger(__name__).warning(
                "[AUIP-LAUNCH-FAILED] request=%s artifact=%s detail=%s",
                clean_request_id, request.artifact_id, str(detail or "")[:1000])
            await self._announce_failure(
                request.session_id,
                "desktop_open_failed",
                detail=detail,
            )
            return {"ok": False, "error": "desktop_open_failed"}
        return {"ok": True, "status": "opened", "artifact_id": request.artifact_id}

    def cancel_deferred(self, *, session_id: str, turn_id: str) -> bool:
        """Retire one not-yet-launched turn continuation after Work admission fails."""

        key = (str(session_id or "").strip(), str(turn_id or "").strip())
        return self._deferred.pop(key, None) is not None

    def authorize_prepare(
        self,
        *,
        session_id: str,
        request_id: str,
        artifact_id: str,
    ) -> bool:
        request = self._launch_requests.get(str(request_id or ""))
        return bool(
            request is not None
            and request.session_id == str(session_id or "")
            and request.artifact_id == str(artifact_id or "")
        )

    async def _request_selection(
        self,
        session_id: str,
        candidates: list[AuipLaunchCandidate],
        mode: str,
    ) -> dict[str, Any]:
        by_option: dict[str, AuipLaunchCandidate] = {}
        options: list[AttentionOption] = []
        for candidate in candidates:
            option_id = opaque_option_id()
            by_option[option_id] = candidate
            options.append(
                AttentionOption(
                    option_id=option_id,
                    label=candidate.title,
                    entity_kind="work_item",
                    description="Open this verified AUIP application",
                    parent_label=candidate.project_title or candidate.work_title,
                    metadata={"scope": "auip_launch", "relation": "experience"},
                )
            )

        async def resume(option_id: str) -> dict[str, Any]:
            return await self._emit_launch(session_id, by_option[option_id], mode)

        request = await self.attention.create_selection(
            session_id=session_id,
            title="Choose an application",
            prompt="More than one verified AUIP application is available. Which one should open?",
            options=options,
            continuation=resume,
            dedupe_key="auip.launch",
        )
        return {"ok": True, "deferred": True, "attention": request}

    async def _request_deferred_work_selection(
        self,
        session_id: str,
        turn_id: str,
        rows: list[dict[str, Any]],
        mode: str,
        input_id: str = "",
        source_app_session_id: str = "",
    ) -> dict[str, Any]:
        """Reuse Attention when more than one active Work can own `after`."""

        by_option: dict[str, dict[str, Any]] = {}
        options: list[AttentionOption] = []
        for row in rows:
            option_id = opaque_option_id()
            by_option[option_id] = row
            options.append(
                AttentionOption(
                    option_id=option_id,
                    label=str(row.get("title") or "Active Work"),
                    entity_kind="work_item",
                    description="Open the AUIP application produced by this active work",
                    metadata={"scope": "auip_after_work", "relation": "running"},
                )
            )

        async def resume(option_id: str) -> dict[str, Any]:
            return await self._bind_deferred_work(
                session_id,
                turn_id,
                by_option[option_id],
                mode,
                input_id,
                source_app_session_id,
            )

        request = await self.attention.create_selection(
            session_id=session_id,
            title="Choose the work to wait for",
            prompt="More than one task is active. Which result should open when it finishes?",
            options=options,
            continuation=resume,
            dedupe_key="auip.launch.after_work",
        )
        return {"ok": True, "deferred": True, "attention": request}

    async def _bind_deferred_work(
        self,
        session_id: str,
        turn_id: str,
        row: dict[str, Any],
        mode: str,
        input_id: str = "",
        source_app_session_id: str = "",
    ) -> dict[str, Any]:
        """Freeze one Operation so steer replacement keeps the continuation."""

        work_item_id = str(row.get("work_item_id") or "").strip()
        operation_id = str(row.get("operation_id") or "").strip()
        if not work_item_id or not operation_id:
            await self._announce_failure(session_id, "deferred_work_unavailable")
            return {"ok": False, "error": "deferred_work_unavailable"}
        attempts = self._attempts_for_operation(work_item_id, operation_id)
        active = [
            item
            for item in attempts
            if str(getattr(item, "execution_status", ""))
            in {"queued", "running", "orphaned"}
        ]
        if active or input_id or source_app_session_id:
            if not attempts:
                await self._announce_failure(session_id, "deferred_work_unavailable")
                return {"ok": False, "error": "deferred_work_unavailable"}
            now = float(self.clock())
            pending = _DeferredLaunch(
                session_id=str(session_id or ""),
                turn_id=str(turn_id or ""),
                mode=mode,
                requested_at=now,
                expires_at=now + DEFERRED_LAUNCH_TTL_S,
                work_item_id=work_item_id,
                operation_id=operation_id,
                input_id=input_id,
                source_app_session_id=source_app_session_id,
            )
            key = (pending.session_id, pending.turn_id)
            self._deferred[key] = pending
            if source_app_session_id and not active and not input_id:
                # A terminal active-Work binding still needs the exact same
                # candidate, delivery and result-entry readiness checks as an
                # update-driven continuation. Preserve the immediate legacy
                # path when there is no captured source.
                await self.on_work_updated(
                    Method.WORK_UPDATED,
                    {},
                    pending_key=key,
                )
            return {
                "ok": True,
                "deferred": True,
                "turn_id": pending.turn_id,
            }
        terminal = [
            item
            for item in attempts
            if str(getattr(item, "execution_status", ""))
            in {"succeeded", "failed", "cancelled"}
        ]
        succeeded_ids = {
            str(item.attempt_id)
            for item in terminal
            if str(item.execution_status) == "succeeded"
        }
        candidate = next(
            (
                item
                for item in self.candidates(session_id)
                if item.work_item_id == work_item_id
                and self._candidate_matches_attempts(item, succeeded_ids)
                and _candidate_supports_mode(item, mode)
            ),
            None,
        )
        if candidate is None:
            if any(
                self._attempt_has_rejected_auip_outcome(attempt)
                for attempt in terminal
            ):
                return {"ok": False, "error": "deferred_delivery_not_launchable"}
            await self._announce_failure(session_id, "deferred_delivery_not_launchable")
            return {"ok": False, "error": "deferred_delivery_not_launchable"}
        return await self._emit_launch(session_id, candidate, mode)

    async def _emit_launch(
        self,
        session_id: str,
        candidate: AuipLaunchCandidate,
        mode: str,
    ) -> dict[str, Any]:
        if candidate.project_id:
            try:
                self.work_roster.destination.available_project(candidate.project_id)
                item = self.artifacts.get_work_item(candidate.work_item_id)
                if item is None or item.project_id != candidate.project_id:
                    raise ValueError("application Project changed")
            except (WorkLedgerError, OSError, ValueError):
                await self._announce_failure(session_id, "app_project_unavailable")
                return {"ok": False, "error": "app_project_unavailable"}
        # Re-discovery prevents an Attention card or a delayed Work callback
        # from opening bytes that changed after the candidate tuple froze.
        current = discover_launchable_auip_app(self.artifacts, candidate.work_item_id)
        if current is None or str(current.get("artifact_id") or "") != candidate.artifact_id:
            await self._announce_failure(session_id, "app_revision_changed")
            return {"ok": False, "error": "app_revision_changed"}
        if mode not in available_engagement_modes(current.get("stances") or ()):
            await self._announce_failure(session_id, "unsupported_launch_mode")
            return {"ok": False, "error": "unsupported_launch_mode"}
        request_id = f"auip_launch_{uuid.uuid4().hex}"
        self._launch_requests[request_id] = _LaunchRequest(
            request_id=request_id,
            session_id=str(session_id or ""),
            artifact_id=candidate.artifact_id,
            created_at=float(self.clock()),
        )
        await self.emit(
            Method.AUIP_LAUNCH_REQUESTED,
            {
                "request_id": request_id,
                "session_id": str(session_id or ""),
                "artifact_id": candidate.artifact_id,
                "work_item_id": candidate.work_item_id,
                "title": candidate.title,
                "mode": mode,
            },
        )
        return {"ok": True, "requested": True, "request_id": request_id}

    def _attempts_for_turn(self, session_id: str, turn_id: str) -> list[Any]:
        rows = self._complete_roster_rows(session_id)
        if rows is None:
            return []
        attempts: list[Any] = []
        for row in rows:
            attempt_id = str(row.get("attempt_id") or "")
            attempt = self.artifacts.get_attempt(attempt_id) if attempt_id else None
            if attempt is None:
                continue
            metadata = attempt.metadata if isinstance(attempt.metadata, dict) else {}
            if str(metadata.get("turn_id") or "") == turn_id:
                attempts.append(attempt)
        return attempts

    def _active_work_rows(
        self,
        session_id: str,
        attempt_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        expected = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in attempt_ids
                if str(value).strip()
            )
        )
        if not expected:
            return []
        roster_rows = self._complete_roster_rows(session_id)
        if roster_rows is None:
            return []
        visible_work_items = {
            str(row.get("work_item_id") or ""): row
            for row in roster_rows
            if str(row.get("work_item_id") or "")
        }
        frozen_rows: list[dict[str, Any]] = []
        for attempt_id in expected:
            attempt = self.artifacts.get_attempt(attempt_id)
            work_item_id = str(getattr(attempt, "work_item_id", "") or "").strip()
            operation_id = str(getattr(attempt, "operation_id", "") or "").strip()
            visible = visible_work_items.get(work_item_id)
            if attempt is None or not work_item_id or not operation_id or visible is None:
                # Never reduce a frozen many-set to one just because a member
                # disappeared or the current roster became unable to prove it.
                return []
            frozen_rows.append(
                {
                    **dict(visible),
                    "attempt_id": attempt_id,
                    "operation_id": operation_id,
                    "execution": str(getattr(attempt, "execution_status", "") or ""),
                }
            )
        return frozen_rows

    def _attempts_for_operation(
        self,
        work_item_id: str,
        operation_id: str,
    ) -> list[Any]:
        list_attempts = getattr(self.artifacts, "list_attempts", None)
        if not callable(list_attempts):
            return []
        return [
            attempt
            for attempt in list_attempts(str(work_item_id or ""))
            if str(getattr(attempt, "operation_id", ""))
            == str(operation_id or "")
        ]

    async def _announce_failure(
        self,
        session_id: str,
        reason: str,
        *,
        detail: str = "",
    ) -> None:
        from server.ai_os_schema import work_note_payload, work_signal
        from server.assistant_language import current_assistant_language
        from server.work_context import add_work_note

        japanese = current_assistant_language() == "japanese"
        summary = (
            "AUIP対応のアプリを確認できなかったため、ゲームは開いていないわ。"
            if japanese and reason in {"no_launchable_auip_app", "deferred_delivery_not_launchable"}
            else "アプリを開けなかったため、ゲームはまだ開始していないわ。"
            if japanese
            else "I could not verify an AUIP-capable application, so nothing was opened."
            if reason in {"no_launchable_auip_app", "deferred_delivery_not_launchable"}
            else "The application could not be opened, so the experience has not started."
        )
        note = work_note_payload(
            source="auip_launch",
            provider="host",
            run_id=f"auip_launch_{time.time_ns()}",
            session_id=str(session_id or ""),
            phase="Result",
            title="AUIP launch did not start",
            summary=summary,
            signals=[
                work_signal(
                    label="launch",
                    text="No AppSession was started",
                    detail=(str(detail or reason)[:240]),
                    kind="status",
                    importance="blocking",
                )
            ],
            importance="blocking",
            metadata={
                "auip_launch_failed": True,
                "reason": reason,
                "execution_started": False,
            },
            speak=True,
        )
        add_work_note(note)
        await self.emit(Method.CHAT_WORK_NOTE, note)


def _mode(value: Any) -> str:
    clean = str(value or "observe").strip().lower()
    return clean if clean in LAUNCH_MODES else "observe"


def _candidate_supports_mode(candidate: AuipLaunchCandidate, mode: str) -> bool:
    return str(mode or "observe") in available_engagement_modes(candidate.stances)


def _matches(
    candidates: list[AuipLaunchCandidate],
    target: str,
) -> list[AuipLaunchCandidate]:
    clean = str(target or "").strip().casefold()
    if not clean:
        return []
    return [
        candidate
        for candidate in candidates
        if clean
        in {
            candidate.title.casefold(),
            candidate.work_title.casefold(),
        }
    ]


def _inline(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())[:160]


_coordinator: AuipLaunchCoordinator | None = None


def set_auip_launch_coordinator(value: AuipLaunchCoordinator | None) -> None:
    global _coordinator
    _coordinator = value


def render_auip_launch_context(
    session_id: str,
    *,
    language: str = "en",
    include_control_contract: bool = True,
) -> str:
    if _coordinator is None:
        return ""
    return _coordinator.render_prompt_context(
        session_id,
        language=language,
        include_control_contract=include_control_contract,
    )
