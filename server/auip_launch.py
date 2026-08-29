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
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

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
from server.event_bus import bus
from server.protocol import Method
from server.work_permission_service import WorkPermissionService


logger = logging.getLogger(__name__)
LAUNCH_MODES = frozenset({"observe", "collaborate", "delegate"})
DEFERRED_LAUNCH_TTL_S = 30 * 60.0


class WorkRoster(Protocol):
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

    def prompt_dict(self) -> dict[str, Any]:
        return {
            "app": self.title,
            "work": self.work_title,
            "modes": [
                "observe",
                *(
                    ["collaborate", "delegate"]
                    if "participant" in self.stances
                    else []
                ),
            ],
        }


@dataclass(frozen=True, slots=True)
class AuipPreparationCandidate:
    """Host-verified runnable Work that needs AUIP before it can launch."""

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


@dataclass(frozen=True, slots=True)
class _LaunchRequest:
    request_id: str
    session_id: str
    artifact_id: str
    created_at: float


Emitter = Callable[[str, dict[str, Any]], Awaitable[None]]
PreparationDispatcher = Callable[
    [AuipPreparationCandidate, str],
    Awaitable[None],
]


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
    ) -> None:
        self.artifacts = artifacts
        self.work_roster = work_roster
        self.attention = attention
        self.emit = emit or bus.emit
        self.clock = clock
        self._deferred: dict[tuple[str, str], _DeferredLaunch] = {}
        self._launch_requests: dict[str, _LaunchRequest] = {}

    def candidates(self, session_id: str, *, limit: int = 8) -> list[AuipLaunchCandidate]:
        roster = self.work_roster.conversation_work_items_for_resolution(
            str(session_id or ""),
            limit=200,
        )
        rows = roster.get("items") if isinstance(roster, dict) else []
        candidates: list[AuipLaunchCandidate] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            work_item_id = str(row.get("work_item_id") or "")
            app = discover_launchable_auip_app(self.artifacts, work_item_id)
            if app is None:
                continue
            app_meta = app.get("app") if isinstance(app.get("app"), dict) else {}
            artifact_id = str(app.get("artifact_id") or "")
            candidates.append(
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
                )
            )
            if len(candidates) >= max(1, min(int(limit), 8)):
                break
        return candidates

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

        roster = self.work_roster.conversation_work_items_for_resolution(
            str(session_id or ""),
            limit=200,
        )
        rows = roster.get("items") if isinstance(roster, dict) else []
        candidates: list[AuipPreparationCandidate] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            work_item_id = str(row.get("work_item_id") or "").strip()
            if not work_item_id or discover_launchable_auip_app(
                self.artifacts,
                work_item_id,
            ) is not None:
                continue
            runnable = list_current_runnable_artifacts(
                self.artifacts,
                work_item_id,
            )
            if not runnable:
                continue
            file_names = tuple(
                dict.fromkeys(
                    str(getattr(record, "path", "")).replace("\\", "/").rsplit("/", 1)[-1]
                    for record in runnable
                    if str(getattr(record, "path", "")).strip()
                )
            )
            candidates.append(
                AuipPreparationCandidate(
                    work_item_id=work_item_id,
                    work_title=str(row.get("title") or "WorkItem"),
                    files=file_names[:3],
                )
            )
            if len(candidates) >= max(1, min(int(limit), 8)):
                break
        return candidates

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
    ) -> dict[str, Any]:
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
            clean_turn = str(turn_id or "").strip()
            if not clean_turn:
                await self._announce_failure(session_id, "launch_turn_unavailable")
                return {"ok": False, "error": "launch_turn_unavailable"}
            binding = str(attrs.get("_host_work_binding") or "turn").strip().lower()
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
                    )
                return await self._bind_deferred_work(
                    session_id,
                    clean_turn,
                    rows[0],
                    mode,
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
        if len(candidates) == 1:
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
        """Resolve one generic runnable delivery before starting ordinary Work."""

        if prepare_work is None:
            await self._announce_failure(session_id, "preparation_unavailable")
            return {"ok": False, "error": "preparation_unavailable"}
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
            await prepare_work(candidate, mode)
        except Exception:
            self._deferred.pop(key, None)
            logger.exception(
                "[AUIP-LAUNCH] preparation dispatch failed session=%s turn=%s",
                clean_session,
                clean_turn,
            )
            await self._announce_failure(session_id, "preparation_start_failed")
            return {"ok": False, "error": "preparation_start_failed"}
        return {
            "ok": True,
            "deferred": True,
            "preparing": True,
            "turn_id": clean_turn,
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

    async def on_work_updated(self, _method: str, payload: dict[str, Any]) -> None:
        if not self._deferred:
            return
        now = float(self.clock())
        for key, pending in list(self._deferred.items()):
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
            if any(
                str(getattr(attempt, "execution_status", ""))
                in {"queued", "running"}
                for attempt in matching_attempts
            ):
                continue
            terminal = [
                attempt
                for attempt in matching_attempts
                if str(getattr(attempt, "execution_status", ""))
                in {"succeeded", "failed", "cancelled", "orphaned"}
            ]
            if not terminal:
                continue
            succeeded_ids = {
                str(attempt.attempt_id)
                for attempt in terminal
                if str(attempt.execution_status) == "succeeded"
            }
            candidate = next(
                (
                    item
                    for item in self.candidates(pending.session_id)
                    if succeeded_ids.intersection(item.contributing_attempt_ids)
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
                    if any(
                        self._attempt_has_rejected_auip_outcome(attempt)
                        for attempt in relevant_attempts
                    ):
                        # Artifact reconciliation can briefly lag a successful
                        # Provider terminal, but a completed Host outcome
                        # verdict is no longer a race. The Work terminal owns
                        # the user-visible error; retire the one-shot launch
                        # without adding a duplicate synthetic failure.
                        self._deferred.pop(key, None)
                        continue
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
            await self._announce_failure(
                request.session_id,
                "desktop_open_failed",
                detail=detail,
            )
            return {"ok": False, "error": "desktop_open_failed"}
        return {"ok": True, "status": "opened", "artifact_id": request.artifact_id}

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
                    parent_label=candidate.work_title,
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
            if str(getattr(item, "execution_status", "")) in {"queued", "running"}
        ]
        if active:
            now = float(self.clock())
            pending = _DeferredLaunch(
                session_id=str(session_id or ""),
                turn_id=str(turn_id or ""),
                mode=mode,
                requested_at=now,
                expires_at=now + DEFERRED_LAUNCH_TTL_S,
                work_item_id=work_item_id,
                operation_id=operation_id,
            )
            self._deferred[(pending.session_id, pending.turn_id)] = pending
            return {
                "ok": True,
                "deferred": True,
                "turn_id": pending.turn_id,
            }
        terminal = [
            item
            for item in attempts
            if str(getattr(item, "execution_status", ""))
            in {"succeeded", "failed", "cancelled", "orphaned"}
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
                and succeeded_ids.intersection(item.contributing_attempt_ids)
            ),
            None,
        )
        if candidate is None:
            await self._announce_failure(session_id, "deferred_delivery_not_launchable")
            return {"ok": False, "error": "deferred_delivery_not_launchable"}
        return await self._emit_launch(session_id, candidate, mode)

    async def _emit_launch(
        self,
        session_id: str,
        candidate: AuipLaunchCandidate,
        mode: str,
    ) -> dict[str, Any]:
        # Re-discovery prevents an Attention card or a delayed Work callback
        # from opening bytes that changed after the candidate tuple froze.
        current = discover_launchable_auip_app(self.artifacts, candidate.work_item_id)
        if current is None or str(current.get("artifact_id") or "") != candidate.artifact_id:
            await self._announce_failure(session_id, "app_revision_changed")
            return {"ok": False, "error": "app_revision_changed"}
        current_stances = {str(value) for value in current.get("stances") or []}
        if mode != "observe" and "participant" not in current_stances:
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
        roster = self.work_roster.conversation_work_items_for_resolution(session_id, limit=200)
        rows = roster.get("items") if isinstance(roster, dict) else []
        attempts: list[Any] = []
        for row in rows if isinstance(rows, list) else []:
            attempt_id = str(row.get("attempt_id") or "") if isinstance(row, dict) else ""
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
        expected = set(attempt_ids)
        if not expected:
            return []
        roster = self.work_roster.conversation_work_items_for_resolution(
            session_id,
            limit=200,
        )
        rows = roster.get("items") if isinstance(roster, dict) else []
        return [
            dict(row)
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
            and str(row.get("attempt_id") or "") in expected
            and str(row.get("execution") or "") in {"queued", "running"}
        ]

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
