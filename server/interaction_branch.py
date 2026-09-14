"""Conversation-level interaction branch coordinator.

This module is intentionally above ProviderRuntime and below main chat.

ProviderRuntime executes work. BrowserAdapter owns Playwright sessions.
InteractionBranchCoordinator owns the short-lived conversation state that lets
the next user turn continue inside a browser branch instead of forcing the main
chat to guess from a thin provider handle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from server.turn_admission import TurnAdmissionRecord

from server.event_bus import bus
from core.turn_coordinator import require_legacy_turn_authority
from server.protocol import Method
from agent_host.provider_outcome import (
    OUTCOME_EVIDENCE_METADATA_KEY,
    ProviderOutcomeEvidence,
)
from agent_host.provider_identity import PARENT_CONTEXT_DELIVERED_EVENT
from server.outcome_verification import (
    ProviderOutcomeVerdict,
    assess_provider_outcome,
)

logger = logging.getLogger(__name__)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


ProviderRunCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ProviderSteerCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ProviderCancelCallable = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class InteractionBranchState:
    branch_id: str
    parent_session_id: str
    provider: str
    status: str
    goal: str
    checkpoint: dict[str, Any] = field(default_factory=dict)
    visible_messages: list[dict[str, Any]] = field(default_factory=list)
    hidden_messages: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    browser_session_id: str = ""
    title: str = ""
    url: str = ""
    page_summary: str = ""
    pending_goal: str = ""
    visible_summary: str = ""
    hidden_summary: str = ""
    completeness: str = "unknown"
    attention: str = "none"
    completion_rationale: str = ""
    merge_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    last_run_id: str = ""
    last_result: str = ""
    active_run_id: str = ""
    # Durable provider-neutral goal identity. Browser page/session state stays
    # branch-local, while consecutive post-run actions append Operations to
    # this WorkItem instead of manufacturing sibling tasks.
    work_item_id: str = ""
    operation_id: str = ""
    instruction_revision: int = 0
    accepted_instruction_revision: int = 0
    applied_instruction_revision: int = 0
    latest_instruction: str = ""
    # squash-merge 区间起点：分支创建时主对话 dialog 的长度。
    # 关闭时从此索引起扫描带本分支 branch_id 标记的条目做坍缩。
    region_start_index: int = -1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InteractionBranchRoutingLease:
    """Immutable turn-start claim over the existing branch authority owner."""

    branch_id: str
    parent_session_id: str
    provider: str
    instruction_revision: int
    expires_at: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(
        cls,
        value: Any,
    ) -> InteractionBranchRoutingLease | None:
        if not isinstance(value, dict):
            return None
        try:
            lease = cls(
                branch_id=str(value.get("branch_id") or "").strip(),
                parent_session_id=str(
                    value.get("parent_session_id") or ""
                ).strip(),
                provider=str(value.get("provider") or "").strip().lower(),
                instruction_revision=int(value.get("instruction_revision") or 0),
                expires_at=float(value.get("expires_at") or 0.0),
            )
        except (TypeError, ValueError):
            return None
        if not lease.branch_id or not lease.parent_session_id or not lease.provider:
            return None
        if not math.isfinite(lease.expires_at) or lease.expires_at <= 0:
            return None
        return lease


@dataclass(frozen=True, slots=True)
class InteractionBranchContinuationReceipt:
    """Host receipt for one attempted continuation against an exact branch."""

    disposition: Literal["accepted", "deferred", "failed", "superseded"]
    reason: str
    branch_id: str
    instruction_revision: int
    run: Mapping[str, Any] = field(default_factory=dict)
    execution_started: bool | None = None

    @property
    def accepted(self) -> bool:
        return self.disposition == "accepted"


class InteractionBranchRunStopUnconfirmed(RuntimeError):
    """An exact branch retired, but its underlying Provider run may still act."""

    def __init__(self, *, branch_id: str, run_id: str, reason: str) -> None:
        self.branch_id = str(branch_id or "")
        self.run_id = str(run_id or "")
        self.reason = str(reason or "run_stop_unconfirmed")
        super().__init__(
            f"browser run stop was not confirmed: {self.reason} "
            f"(branch={self.branch_id}, run={self.run_id})"
        )


class InteractionBranchRoutingLeaseStale(RuntimeError):
    """A turn may not retire or replace a newer branch generation."""

    def __init__(self, lease: InteractionBranchRoutingLease) -> None:
        self.branch_id = lease.branch_id
        self.reason = "stale_turn_start_lease"
        super().__init__(
            "captured Browser routing lease is no longer current "
            f"(branch={lease.branch_id}, revision={lease.instruction_revision})"
        )


@dataclass(frozen=True, slots=True)
class _PendingBranchTermination:
    branch_id: str
    run_id: str
    reason: str
    observed_at: float


@dataclass(frozen=True, slots=True)
class _ProviderAdmissionReservation:
    reservation_id: str
    provider: str
    observed_at: float
    run_id: str = ""


class InteractionBranchCoordinator:
    """Track active conversation branches and route continuation turns.

    The first implementation is deliberately conservative and browser-focused.
    It solves the "open page, then continue operating on that page" failure
    without introducing a new browser engine or injecting raw DOM into main
    chat.
    """

    def __init__(
        self,
        *,
        provider_run: ProviderRunCallable,
        provider_steer: ProviderSteerCallable | None = None,
        provider_cancel: ProviderCancelCallable | None = None,
        root: str | Path = Path("runtime") / "interaction_branches",
        ttl_seconds: float = 900.0,
        display_language: Callable[[], str] | None = None,
    ) -> None:
        self.provider_run = provider_run
        self.provider_steer = provider_steer
        self.provider_cancel = provider_cancel
        self.root = Path(root)
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self._get_display_language = display_language
        self._active_by_session: dict[str, InteractionBranchState] = {}
        self._branch_locks: dict[str, asyncio.Lock] = {}
        self._closed_run_until: dict[str, float] = {}
        self._closed_branch_until: dict[str, float] = {}
        self._termination_pending_by_session: dict[
            str,
            dict[str, _PendingBranchTermination],
        ] = {}
        self._provider_admission_by_session: dict[
            str,
            _ProviderAdmissionReservation,
        ] = {}
        self._subscribed = False

    def configure(self) -> None:
        global _current_coordinator
        if self._subscribed:
            return
        bus.on(Method.PROVIDER_RESULT, self._on_provider_result)
        bus.on(Method.PROVIDER_EVENT, self._on_provider_event)
        self._subscribed = True
        # 模块级单例注册：供 work_context（分支状态块注入）与
        # _handle_delegate（branch=continue/close 意图消费）访问
        _current_coordinator = self

    # ── 主 LLM 路由接口（单脑路由改造，2026-07-04）─────────────────────────
    #
    # 路由权归属主对话 LLM：它通过 DELEGATE 标签的 branch 属性表达
    # continue / new / close 意图。本协调器只保留三条"结构性"快通道
    # （按分支状态而非词表触发），其余一切用户消息直接落回主对话，
    # 由主 LLM 在分支状态块（work_context 注入）的辅助下决策。

    def active_branch_for_session(self, session_id: str) -> InteractionBranchState | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        branch = self._active_by_session.get(sid)
        if branch is None:
            return None
        if self._is_expired(branch):
            self._close_branch(branch, status="stale", reason="ttl_expired")
            return None
        return branch

    def capture_routing_lease(
        self,
        session_id: str,
    ) -> InteractionBranchRoutingLease | None:
        """Freeze the live branch identity seen by one admitted user turn."""

        branch = self.active_branch_for_session(session_id)
        if branch is None or branch.provider != "browser" or not (
            branch.browser_session_id or branch.active_run_id
        ):
            return None
        if not math.isfinite(branch.expires_at) or branch.expires_at <= 0:
            return None
        return InteractionBranchRoutingLease(
            branch_id=branch.branch_id,
            parent_session_id=branch.parent_session_id,
            provider=branch.provider,
            instruction_revision=branch.instruction_revision,
            expires_at=branch.expires_at,
        )

    def resolve_routing_lease(
        self,
        lease: InteractionBranchRoutingLease,
    ) -> InteractionBranchState | None:
        """Resolve only the exact still-current branch generation."""

        if not isinstance(lease, InteractionBranchRoutingLease):
            return None
        branch = self.active_branch_for_session(lease.parent_session_id)
        if branch is None:
            return None
        now = time.time()
        if (
            branch.branch_id != lease.branch_id
            or branch.provider != lease.provider
            or branch.instruction_revision != lease.instruction_revision
            or not math.isfinite(lease.expires_at)
            or lease.expires_at <= now
            or not math.isfinite(branch.expires_at)
            or branch.expires_at <= now
            or not (branch.browser_session_id or branch.active_run_id)
        ):
            return None
        return branch

    async def validate_routing_lease(
        self,
        lease: InteractionBranchRoutingLease,
    ) -> bool:
        """Revalidate one captured generation under the owning Session lock."""

        lock = self._branch_locks.setdefault(lease.parent_session_id, asyncio.Lock())
        async with lock:
            return bool(
                not self.termination_pending_for_session(lease.parent_session_id)
                and self.resolve_routing_lease(lease) is not None
            )

    async def validate_absent_routing_scope(self, session_id: str) -> bool:
        """Confirm that no branch appeared after an admitted empty snapshot."""

        sid = str(session_id or "").strip()
        if not sid:
            return False
        lock = self._branch_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            return bool(
                self.active_branch_for_session(sid) is None
                and not self.termination_pending_for_session(sid)
                and self.provider_admission_for_session(sid) is None
            )

    async def provider_start_admission(
        self,
        scope: Mapping[str, Any],
        *,
        session_id: str,
        provider: str,
        reservation_id: str,
        run_id: str = "",
        phase: str,
    ) -> tuple[bool, str]:
        """Reserve/validate one short final-Provider admission window."""

        sid = str(session_id or "").strip()
        token = str(reservation_id or "").strip()
        selected = str(provider or "").strip().lower()
        step = str(phase or "").strip().lower()
        scope_sid = str(scope.get("parent_session_id") or "").strip()
        state = str(scope.get("state") or "bound").strip().lower()
        if not sid or scope_sid != sid or not token or not selected:
            return False, "invalid_provider_admission_identity"
        lock = self._branch_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            existing = self.provider_admission_for_session(sid)
            if step == "release":
                if existing is not None and existing.reservation_id == token:
                    self._provider_admission_by_session.pop(sid, None)
                return True, "provider_admission_released"
            if self.termination_pending_for_session(sid):
                return False, "prior_browser_run_stop_unconfirmed"
            if state != "absent":
                return False, "provider_admission_requires_absent_browser_scope"
            branch = self.active_branch_for_session(sid)
            if step == "reserve":
                if existing is not None and existing.reservation_id != token:
                    return False, "provider_admission_already_reserved"
                if existing is not None and existing.provider != selected:
                    return False, "provider_admission_provider_mismatch"
                if branch is not None:
                    return False, "browser_branch_appeared_before_provider_admission"
                self._provider_admission_by_session[sid] = (
                    _ProviderAdmissionReservation(
                        reservation_id=token,
                        provider=selected,
                        observed_at=time.time(),
                    )
                )
                return True, "provider_admission_reserved"
            if existing is None or existing.reservation_id != token:
                return False, "provider_admission_reservation_lost"
            if existing.provider != selected:
                return False, "provider_admission_provider_mismatch"
            if step == "created":
                exact_run_id = str(run_id or "").strip()
                if not exact_run_id:
                    return False, "provider_admission_run_id_missing"
                self._provider_admission_by_session[sid] = (
                    _ProviderAdmissionReservation(
                        reservation_id=existing.reservation_id,
                        provider=existing.provider,
                        observed_at=existing.observed_at,
                        run_id=exact_run_id,
                    )
                )
                return True, "provider_admission_run_bound"
            own_browser_run = bool(
                selected == "browser"
                and str(run_id or "").strip()
                and branch is not None
                and branch.active_run_id == str(run_id or "").strip()
                and existing.run_id == str(run_id or "").strip()
            )
            if branch is not None and not own_browser_run:
                return False, "browser_branch_appeared_during_provider_admission"
            if step == "commit":
                self._provider_admission_by_session.pop(sid, None)
                return True, "provider_admission_committed"
            return False, "invalid_provider_admission_phase"

    def provider_admission_for_session(
        self,
        session_id: str,
    ) -> _ProviderAdmissionReservation | None:
        sid = str(session_id or "").strip()
        reservation = self._provider_admission_by_session.get(sid)
        if reservation is not None and (
            time.time() - reservation.observed_at
        ) > max(300.0, self.ttl_seconds * 2.0):
            logger.error(
                "expiring orphaned provider admission reservation session=%s token=%s",
                sid,
                reservation.reservation_id,
            )
            self._provider_admission_by_session.pop(sid, None)
            return None
        return reservation

    async def close_from_routing_lease(
        self,
        lease: InteractionBranchRoutingLease,
        *,
        reason: str,
        admission_check: Callable[[], None] | None = None,
    ) -> bool:
        """Close the exact captured branch; never a later replacement."""

        lock = self._branch_locks.setdefault(lease.parent_session_id, asyncio.Lock())
        cancel_args: tuple[str, str, str, int] | None = None
        async with lock:
            if admission_check is not None:
                admission_check()
            pending = self.termination_pending_for_session(
                lease.parent_session_id
            )
            if pending:
                first = pending[0]
                raise InteractionBranchRunStopUnconfirmed(
                    branch_id=first.branch_id,
                    run_id=first.run_id,
                    reason=first.reason,
                )
            branch = self.resolve_routing_lease(lease)
            if branch is None:
                return False
            active_run_id = str(branch.active_run_id or "").strip()
            browser_session_id = branch.browser_session_id
            self._close_branch(
                branch,
                status="closed",
                reason=reason,
                queue_stop=False,
            )
            if active_run_id:
                cancel_args = (
                    branch.branch_id,
                    browser_session_id,
                    active_run_id,
                    branch.instruction_revision,
                )
                self._mark_termination_pending(
                    session_id=lease.parent_session_id,
                    branch_id=branch.branch_id,
                    run_id=active_run_id,
                    reason=f"termination_in_progress:{reason}",
                )
        if cancel_args is not None:
            branch_id, browser_session_id, run_id, revision = cancel_args
            confirmed, stop_reason, _before_execution = await self._cancel_stale_run_identity(
                session_id=lease.parent_session_id,
                branch_id=branch_id,
                browser_session_id=browser_session_id,
                run_id=run_id,
                revision=revision,
                reason=reason,
            )
            if not confirmed:
                raise InteractionBranchRunStopUnconfirmed(
                    branch_id=branch_id,
                    run_id=run_id,
                    reason=stop_reason,
                )
        return True

    async def close_active_branch(
        self,
        session_id: str,
        *,
        reason: str = "llm_close",
    ) -> bool:
        """主 LLM 发出 branch=close：关闭当前会话的活跃分支。"""
        sid = str(session_id or "").strip()
        if not sid:
            return False
        lock = self._branch_locks.setdefault(sid, asyncio.Lock())
        cancel_args: tuple[str, str, str, int] | None = None
        async with lock:
            pending = self.termination_pending_for_session(sid)
            if pending:
                first = pending[0]
                raise InteractionBranchRunStopUnconfirmed(
                    branch_id=first.branch_id,
                    run_id=first.run_id,
                    reason=first.reason,
                )
            branch = self.active_branch_for_session(sid)
            if branch is None:
                return False
            active_run_id = str(branch.active_run_id or "").strip()
            browser_session_id = branch.browser_session_id
            self._close_branch(
                branch,
                status="closed",
                reason=reason,
                queue_stop=False,
            )
            if active_run_id:
                cancel_args = (
                    branch.branch_id,
                    browser_session_id,
                    active_run_id,
                    branch.instruction_revision,
                )
                self._mark_termination_pending(
                    session_id=sid,
                    branch_id=branch.branch_id,
                    run_id=active_run_id,
                    reason=f"termination_in_progress:{reason}",
                )
        if cancel_args is not None:
            branch_id, browser_session_id, run_id, revision = cancel_args
            confirmed, stop_reason, _before_execution = await self._cancel_stale_run_identity(
                session_id=sid,
                branch_id=branch_id,
                browser_session_id=browser_session_id,
                run_id=run_id,
                revision=revision,
                reason=reason,
            )
            if not confirmed:
                raise InteractionBranchRunStopUnconfirmed(
                    branch_id=branch_id,
                    run_id=run_id,
                    reason=stop_reason,
                )
        return True

    async def close_for_provider_handoff(
        self,
        session_id: str,
        *,
        next_provider: str,
        routing_lease: InteractionBranchRoutingLease | None = None,
        replace_same_provider: bool = False,
    ) -> bool:
        """Retire a live branch when canonical control selects another Provider.

        An interaction branch is an execution context, not the Session's
        routing authority. Keeping it active after a Provider handoff lets its
        structural fast paths steal later turns from the newly selected context.
        Same-provider work remains on the normal continue/new/close lifecycle.
        """

        sid = str(session_id or "").strip()
        selected = str(next_provider or "").strip().lower()
        if not sid or not selected:
            return False
        lock = self._branch_locks.setdefault(sid, asyncio.Lock())
        cancel_args: tuple[str, str, str, int] | None = None
        async with lock:
            pending = self.termination_pending_for_session(sid)
            if pending:
                first = pending[0]
                raise InteractionBranchRunStopUnconfirmed(
                    branch_id=first.branch_id,
                    run_id=first.run_id,
                    reason=first.reason,
                )
            if routing_lease is not None:
                if routing_lease.parent_session_id != sid:
                    raise InteractionBranchRoutingLeaseStale(routing_lease)
                branch = self.resolve_routing_lease(routing_lease)
                if branch is None:
                    raise InteractionBranchRoutingLeaseStale(routing_lease)
            else:
                branch = self.active_branch_for_session(sid)
            if branch is None or (
                branch.provider == selected and not replace_same_provider
            ):
                return False
            active_run_id = str(branch.active_run_id or "").strip()
            browser_session_id = branch.browser_session_id
            self._close_branch(
                branch,
                status="superseded",
                reason=f"provider_handoff:{selected}",
                queue_stop=False,
            )
            if active_run_id:
                cancel_args = (
                    branch.branch_id,
                    browser_session_id,
                    active_run_id,
                    branch.instruction_revision,
                )
                self._mark_termination_pending(
                    session_id=sid,
                    branch_id=branch.branch_id,
                    run_id=active_run_id,
                    reason=f"termination_in_progress:provider_handoff:{selected}",
                )
        if cancel_args is not None:
            branch_id, browser_session_id, run_id, revision = cancel_args
            confirmed, stop_reason, _before_execution = await self._cancel_stale_run_identity(
                session_id=sid,
                branch_id=branch_id,
                browser_session_id=browser_session_id,
                run_id=run_id,
                revision=revision,
                reason=f"provider_handoff:{selected}",
            )
            if not confirmed:
                raise InteractionBranchRunStopUnconfirmed(
                    branch_id=branch_id,
                    run_id=run_id,
                    reason=stop_reason,
                )
        return True

    async def continue_from_delegate(
        self,
        *,
        session_id: str,
        task: str,
        source_user_text: str = "",
        turn_id: str = "",
        routing_lease: InteractionBranchRoutingLease | None = None,
        admission_check: Callable[[], None] | None = None,
    ) -> InteractionBranchContinuationReceipt | None:
        """主 LLM 发出 branch=continue：在活跃分支内后台执行规范化指令。

        与旧的同步路由不同：不 await run 完成——发标签的那轮对话已经给了
        用户即时应答。分支终态由 PROVIDER_RESULT 订阅写回；Browser snapshot
        仍保持静音，后续读取只能使用经过宿主事实约束的 visible_summary。
        返回 run 信息；无活跃分支时返回 None（调用方按 branch=new 处理）。
        """
        sid = str(session_id or "").strip()
        if self.termination_pending_for_session(sid):
            return None
        if routing_lease is not None and sid != routing_lease.parent_session_id:
            return None
        branch = (
            self.resolve_routing_lease(routing_lease)
            if routing_lease is not None
            else self.active_branch_for_session(sid)
        )
        if branch is None or branch.provider != "browser" or not (
            branch.browser_session_id or branch.active_run_id
        ):
            return None
        if not math.isfinite(branch.expires_at) or branch.expires_at <= time.time():
            return None
        # The model-authored task is an execution proposal.  The exact source
        # turn owns the branch goal and visible transcript; otherwise one bad
        # paraphrase becomes durable "user" context and is amplified by every
        # later continuation.
        user_text = str(source_user_text or task or "").strip()
        if not user_text:
            return None
        return await self._continue_branch(
            branch,
            user_text,
            turn_id=turn_id,
            route_reason="llm_branch_continue",
            message_source=(
                "main_chat_intervention"
                if str(source_user_text or "").strip()
                else "legacy_llm_branch_continue"
            ),
            routing_lease=routing_lease,
            admission_check=admission_check,
        )

    async def start_from_turn(
        self,
        *,
        session_id: str,
        source_user_text: str,
        target_url: str,
        turn_id: str,
        routing_scope: Mapping[str, Any],
        admission_check: Callable[[], None] | None = None,
    ) -> InteractionBranchContinuationReceipt | None:
        """Start one Browser branch only from an exact admitted absent scope."""

        sid = str(session_id or "").strip()
        text = str(source_user_text or "").strip()
        url = str(target_url or "").strip()
        parsed = urlparse(url)
        if (not sid or not text or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or str(routing_scope.get("state") or "") != "absent"
                or str(routing_scope.get("parent_session_id") or "") != sid):
            return None
        if not await self.validate_absent_routing_scope(sid):
            return None
        if admission_check is not None:
            admission_check()
        from server.provider_requirements import (
            DelegateRequirementFacts,
            compile_delegate_requirements,
        )

        branch_id = f"ibr_browser_{uuid.uuid4().hex[:16]}"
        admission_id = f"browser-admission-{uuid.uuid4().hex}"
        requirements = compile_delegate_requirements(
            DelegateRequirementFacts(
                requested_provider="browser",
                required_interaction="bidirectional",
            )
        )
        response = await self.provider_run({"provider":"browser",
            "task":text, "mode":"open", "requirements":requirements.to_dict(),
            "metadata":{"source":"llm_delegate", "session_id":sid,
                "turn_id":str(turn_id or ""), "source_user_text":text,
                "browser_action":"open", "browser_mode":"open", "url":url,
                "provider_branch":True, "branch_intent":"new",
                "interaction_branch_id":branch_id,
                "branch_instruction_revision":1,
                "interaction_branch_routing_scope":dict(routing_scope),
                "interaction_branch_admission_id":admission_id,
                "max_branch_actions":0}})
        run = response.get("run") if isinstance(response, dict) else {}
        run = dict(run) if isinstance(run, dict) else {}
        if not str(run.get("run_id") or ""):
            return InteractionBranchContinuationReceipt(
                disposition="failed", reason="provider_start_receipt_missing",
                branch_id=branch_id, instruction_revision=1,
                execution_started=None)
        return InteractionBranchContinuationReceipt(
            disposition="accepted", reason="provider_run_started",
            branch_id=branch_id, instruction_revision=1, run=run)

    async def _continue_branch(
        self,
        branch: InteractionBranchState,
        user_text: str,
        *,
        turn_id: str,
        route_reason: str,
        message_source: str,
        routing_lease: InteractionBranchRoutingLease | None = None,
        admission_check: Callable[[], None] | None = None,
    ) -> InteractionBranchContinuationReceipt | None:
        """Reserve under the session lock, await Provider I/O, then finalize."""

        lock = self._branch_locks.setdefault(branch.parent_session_id, asyncio.Lock())
        async with lock:
            if admission_check is not None:
                admission_check()
            if self.termination_pending_for_session(branch.parent_session_id):
                return None
            current = (
                self.resolve_routing_lease(routing_lease)
                if routing_lease is not None
                else self.active_branch_for_session(branch.parent_session_id)
            )
            if current is None or current.branch_id != branch.branch_id:
                return None
            self._append_branch_message(
                current,
                role="user",
                content=user_text,
                visibility="visible",
                source=message_source,
                metadata={"turn_id": turn_id},
            )
            current.status = "active"
            if not current.pending_goal:
                current.pending_goal = self._trim(user_text, 700)
            current.updated_at = time.time()
            current.expires_at = current.updated_at + self.ttl_seconds
            current.instruction_revision += 1
            current.latest_instruction = self._trim(user_text, 700)
            revision = current.instruction_revision
            params = self._build_continue_params(
                current,
                user_text,
                turn_id=turn_id,
                route_reason=route_reason,
            )
            active_run_id = str(current.active_run_id or "")
            self._persist(current)
            logger.info(
                "routing branch continuation session=%s branch=%s reason=%s task=%r",
                current.parent_session_id,
                current.branch_id,
                route_reason,
                user_text[:60],
            )
        return await self._start_or_steer(
                current,
                params,
                turn_id=turn_id,
                revision=revision,
                active_run_id=active_run_id,
            )

    async def _start_or_steer(
        self,
        branch: InteractionBranchState,
        params: dict[str, Any],
        *,
        turn_id: str,
        revision: int,
        active_run_id: str,
    ) -> InteractionBranchContinuationReceipt:
        """Run Provider calls outside the lock and commit only the reservation."""

        lock = self._branch_locks.setdefault(branch.parent_session_id, asyncio.Lock())
        if active_run_id and self.provider_steer is None:
            async with lock:
                if self._reservation_is_current(branch, revision):
                    branch.metadata = {
                        **branch.metadata,
                        "steering": {
                            "state": "failed",
                            "revision": revision,
                            "run_id": active_run_id,
                            "reason": "provider_steering_unavailable",
                        },
                    }
                    self._persist(branch)
            return InteractionBranchContinuationReceipt(
                disposition="failed",
                reason="provider_steering_unavailable",
                branch_id=branch.branch_id,
                instruction_revision=revision,
                run={
                    "run_id": active_run_id,
                    "provider": branch.provider,
                    "status": "running",
                },
            )
        if active_run_id and self.provider_steer is not None:
            try:
                steer_result = await self.provider_steer(
                    {
                        "run_id": active_run_id,
                        "task": params["task"],
                        "revision": revision,
                        "metadata": dict(params["metadata"]),
                    }
                )
            except Exception as exc:
                logger.exception("failed to steer browser interaction branch")
                return InteractionBranchContinuationReceipt(
                    disposition="failed",
                    reason=f"provider_steer_failed:{type(exc).__name__}",
                    branch_id=branch.branch_id,
                    instruction_revision=revision,
                )
            async with lock:
                if not self._reservation_is_current(branch, revision):
                    return InteractionBranchContinuationReceipt(
                        disposition="superseded",
                        reason="branch_generation_superseded_during_steer",
                        branch_id=branch.branch_id,
                        instruction_revision=revision,
                        run=(
                            dict(steer_result.get("run"))
                            if isinstance(steer_result, dict)
                            and isinstance(steer_result.get("run"), dict)
                            else {}
                        ),
                    )
                if (
                    isinstance(steer_result, dict)
                    and steer_result.get("accepted") is True
                ):
                    branch.accepted_instruction_revision = max(
                        branch.accepted_instruction_revision,
                        revision,
                    )
                    branch.metadata = {
                        **branch.metadata,
                        "steering": {
                            "state": "queued",
                            "revision": revision,
                            "run_id": active_run_id,
                            "turn_id": turn_id,
                        },
                    }
                    branch.updated_at = time.time()
                    self._persist(branch)
                    supplied_run = steer_result.get("run")
                    run = (
                        dict(supplied_run)
                        if isinstance(supplied_run, dict)
                        else {
                            "run_id": active_run_id,
                            "provider": branch.provider,
                            "status": "running",
                        }
                    )
                    return InteractionBranchContinuationReceipt(
                        disposition="accepted",
                        reason="steer_accepted",
                        branch_id=branch.branch_id,
                        instruction_revision=revision,
                        run=run,
                    )

                reason = str(
                    steer_result.get("reason")
                    if isinstance(steer_result, dict)
                    else ""
                )
                supplied_run = (
                    steer_result.get("run")
                    if isinstance(steer_result, dict)
                    else {}
                )
                run = dict(supplied_run) if isinstance(supplied_run, dict) else {}
                run_status = str(run.get("status") or "").lower()
                if reason not in {"already_finished", "not_found"} and run_status in {
                    "queued",
                    "running",
                }:
                    # Never start a second run against the same Playwright
                    # session. Rejection is a visible next-turn deferral, not
                    # a successful continuation receipt.
                    branch.metadata = {
                        **branch.metadata,
                        "steering": {
                            "state": "deferred",
                            "revision": revision,
                            "run_id": active_run_id,
                            "reason": reason or "active_run_rejected_steer",
                        },
                    }
                    self._persist(branch)
                    return InteractionBranchContinuationReceipt(
                        disposition="deferred",
                        reason=reason or "active_run_rejected_steer",
                        branch_id=branch.branch_id,
                        instruction_revision=revision,
                        run=run,
                    )
                branch.active_run_id = ""
                self._persist(branch)

        try:
            response = await self.provider_run(params)
        except Exception as exc:
            logger.exception("failed to start browser interaction branch continuation")
            return InteractionBranchContinuationReceipt(
                disposition="failed",
                reason=f"provider_start_failed:{type(exc).__name__}",
                branch_id=branch.branch_id,
                instruction_revision=revision,
            )
        run = response.get("run") if isinstance(response, dict) else {}
        if not isinstance(run, dict):
            run = {}
        run_id = str(run.get("run_id") or "")
        retained_original_run = False
        before_execution: bool | None = None
        async with lock:
            if not self._reservation_is_current(branch, revision):
                current = self._active_by_session.get(branch.parent_session_id)
                same_run_in_newer_generation = bool(
                    current is not None
                    and current.branch_id == branch.branch_id
                    and current.active_run_id == run_id
                    and current.instruction_revision > revision
                    and not self._branch_was_semantically_closed(branch.branch_id)
                )
                adopted_by_newer_generation = bool(
                    same_run_in_newer_generation
                    and current is not None
                    and current.accepted_instruction_revision > revision
                )
                retained_original_run = bool(
                    same_run_in_newer_generation
                    and not adopted_by_newer_generation
                )
                cancel_stale = bool(
                    not retained_original_run
                    and not adopted_by_newer_generation
                    and not self._run_was_semantically_closed(run_id)
                )
                if cancel_stale:
                    self._mark_run_semantically_closed(run_id)
                    self._mark_termination_pending(
                        session_id=branch.parent_session_id,
                        branch_id=branch.branch_id,
                        run_id=run_id,
                        reason=(
                            "termination_in_progress:"
                            "branch_generation_superseded_during_start"
                        ),
                    )
                stale = not retained_original_run
            else:
                branch.active_run_id = run_id
                branch.status = "active"
                branch.accepted_instruction_revision = max(
                    branch.accepted_instruction_revision,
                    revision,
                )
                branch.updated_at = time.time()
                self._persist(branch)
                stale = False
        if retained_original_run:
            return InteractionBranchContinuationReceipt(
                disposition="accepted",
                reason="provider_run_retained_after_newer_continuation_rejected",
                branch_id=branch.branch_id,
                instruction_revision=revision,
                run=dict(run),
            )
        if stale:
            if cancel_stale:
                confirmed, stop_reason, before_execution = await self._cancel_stale_run(
                    branch,
                    run_id=run_id,
                    revision=revision + 1,
                    reason="branch_generation_superseded_during_start",
                )
                if not confirmed:
                    return InteractionBranchContinuationReceipt(
                        disposition="failed",
                        reason=f"run_stop_unconfirmed:{stop_reason}",
                        branch_id=branch.branch_id,
                        instruction_revision=revision,
                        run=dict(run),
                        execution_started=None,
                    )
            return InteractionBranchContinuationReceipt(
                disposition="superseded",
                reason="branch_generation_superseded_during_start",
                branch_id=branch.branch_id,
                instruction_revision=revision,
                run=dict(run),
                execution_started=(False if before_execution is True else None),
            )
        return InteractionBranchContinuationReceipt(
            disposition="accepted",
            reason="provider_run_started",
            branch_id=branch.branch_id,
            instruction_revision=revision,
            run=dict(run),
        )

    def _reservation_is_current(
        self,
        branch: InteractionBranchState,
        revision: int,
    ) -> bool:
        current = self._active_by_session.get(branch.parent_session_id)
        return bool(
            current is not None
            and current.branch_id == branch.branch_id
            and current.instruction_revision == revision
            and not self._branch_was_semantically_closed(branch.branch_id)
        )

    def _build_continue_params(
        self,
        branch: InteractionBranchState,
        user_text: str,
        *,
        turn_id: str,
        route_reason: str,
    ) -> dict[str, Any]:
        from server.provider_requirements import (
            DelegateRequirementFacts,
            compile_delegate_requirements,
        )

        task = self._branch_task(branch, user_text)
        requirements = compile_delegate_requirements(
            DelegateRequirementFacts(
                requested_provider=branch.provider,
                required_steering="immediate",
                required_interaction="bidirectional",
            )
        )
        work_binding = (
            {"work_item_id": branch.work_item_id}
            if branch.work_item_id
            else {}
        )
        return {
            "provider": "browser",
            "task": task,
            "mode": "observe",
            "requirements": requirements.to_dict(),
            "metadata": {
                "source": "llm_delegate",
                "session_id": branch.parent_session_id,
                "intent": "amend" if work_binding else "execute",
                **({"continuation": "amend", "work": work_binding} if work_binding else {}),
                "turn_id": turn_id,
                "interaction_branch_id": branch.branch_id,
                "branch_intent": "continue",
                "provider_branch": True,
                "browser_action": "observe",
                "browser_mode": "observe",
                "browser_session_id": branch.browser_session_id,
                "max_branch_actions": 3,
                "branch_parent_goal": branch.goal,
                "branch_pending_goal": branch.pending_goal,
                "branch_user_message": user_text,
                "conversation_checkpoint": dict(branch.checkpoint),
                "branch_visible_messages": list(branch.visible_messages[-10:]),
                "branch_hidden_summary": branch.hidden_summary,
                "branch_route_reason": route_reason,
                "branch_instruction_revision": branch.instruction_revision,
            },
        }

    async def try_route_user_message(
        self,
        *,
        text: str,
        session_id: str,
        turn_id: str = "",
        routing_scope: Mapping[str, Any] | None = None,
        turn_admission: TurnAdmissionRecord | None = None,
    ) -> dict[str, Any] | None:
        """Route a user turn into the active branch when it is a continuation."""

        require_legacy_turn_authority(turn_admission)
        if turn_admission is not None:
            session_id = turn_admission.session_id
        user_text = str(text or "").strip()
        sid = str(session_id or "").strip()
        if not user_text or not sid:
            return None
        if self.termination_pending_for_session(sid):
            return None
        routing_lease: InteractionBranchRoutingLease | None = None
        if routing_scope is not None:
            scope_state = str(routing_scope.get("state") or "").strip().lower()
            if scope_state != "bound":
                # An admitted absence/quarantine is evidence, not permission to
                # adopt a branch that appeared while this turn was in flight.
                return None
            routing_lease = InteractionBranchRoutingLease.from_mapping(routing_scope)
            if routing_lease is None or routing_lease.parent_session_id != sid:
                return None
            branch = self.resolve_routing_lease(routing_lease)
        else:
            # Compatibility for direct callers predating turn-start capture.
            # Production ChatHandler always supplies the admitted scope.
            branch = self.active_branch_for_session(sid)
        if branch is None:
            return None
        if branch.provider != "browser" or not (
            branch.browser_session_id or branch.active_run_id
        ):
            return None
        if not math.isfinite(branch.expires_at) or branch.expires_at <= time.time():
            return None
        # 三条结构性快通道（按分支状态/显式结构触发，不查词表）。
        # 其余一切消息返回 None → 落回主对话，由主 LLM 借助分支状态块
        # 决定 branch=continue/new/close（单脑路由）。
        route_kind, route_reason = self._structural_fast_path(branch, user_text)
        if route_kind == "retarget":
            lock = self._branch_locks.setdefault(sid, asyncio.Lock())
            cancel_args: tuple[str, str, str, int] | None = None
            transitioned_branch_id = ""
            async with lock:
                current = (
                    self.resolve_routing_lease(routing_lease)
                    if routing_lease is not None
                    else self._active_by_session.get(sid)
                )
                if current is not None and current.branch_id == branch.branch_id:
                    transitioned_branch_id = current.branch_id
                    active_run_id = str(current.active_run_id or "").strip()
                    browser_session_id = current.browser_session_id
                    self._close_branch(
                        current,
                        status="superseded",
                        reason=route_reason,
                        queue_stop=False,
                    )
                    if active_run_id:
                        cancel_args = (
                            current.branch_id,
                            browser_session_id,
                            active_run_id,
                            current.instruction_revision,
                        )
                        self._mark_termination_pending(
                            session_id=sid,
                            branch_id=current.branch_id,
                            run_id=active_run_id,
                            reason=f"termination_in_progress:{route_reason}",
                        )
            if cancel_args is not None:
                branch_id, browser_session_id, run_id, revision = cancel_args
                confirmed, stop_reason, _before_execution = await self._cancel_stale_run_identity(
                    session_id=sid,
                    branch_id=branch_id,
                    browser_session_id=browser_session_id,
                    run_id=run_id,
                    revision=revision,
                    reason=route_reason,
                )
                if not confirmed:
                    return {
                        "handled": True,
                        "route_kind": "browser_retarget_blocked",
                        "branch_id": "",
                        "blocked_branch_id": branch_id,
                        "provider": "browser",
                        "display_text": (
                            "I could not confirm that the previous browser action stopped, "
                            "so I did not start the new page request."
                        ),
                        "voice_text_ja": (
                            "前のブラウザ操作が停止したことを確認できなかったため、"
                            "新しいページ操作は開始していないわ。"
                        ),
                        "speak": True,
                        "continuation_disposition": "failed",
                        "continuation_reason": (
                            f"run_stop_unconfirmed:{stop_reason}"
                        ),
                    }
            if not transitioned_branch_id:
                return None
            return {
                "handled": False,
                "route_kind": "browser_retarget_released",
                "provider": "browser",
                "routing_scope_transition": {
                    "state": "absent",
                    "parent_session_id": sid,
                    "captured_at": time.time(),
                    "transitioned_from_branch_id": transitioned_branch_id,
                },
            }
        if route_kind != "continue":
            return None

        receipt = await self._continue_branch(
            branch,
            user_text,
            turn_id=turn_id,
            route_reason=route_reason,
            message_source="branch_followup",
            routing_lease=routing_lease,
        )
        if receipt is None:
            return None
        run = dict(receipt.run)
        if not receipt.accepted:
            execution_uncertain = receipt.execution_started is not False
            return {
                "handled": True,
                "route_kind": "browser_continuation_blocked",
                "branch_id": receipt.branch_id,
                "provider": "browser",
                "display_text": (
                    "I could not confirm whether the current browser transition "
                    "had already begun, so I blocked any replacement action."
                    if execution_uncertain
                    else (
                        "I could not apply that instruction to the current browser run. "
                        "Nothing new was started; please try again after the current step finishes."
                    )
                ),
                "voice_text_ja": (
                    "現在のブラウザ遷移がすでに始まっていたか確認できなかったため、"
                    "代わりの操作は開始せずに止めたわ。"
                    if execution_uncertain
                    else (
                        "いまのブラウザ操作にはその指示を適用できなかったわ。"
                        "新しい処理は開始していないから、現在の操作が終わってからもう一度頼んで。"
                    )
                ),
                "speak": True,
                "run": run,
                "continuation_disposition": receipt.disposition,
                "continuation_reason": receipt.reason,
                **(
                    {"execution_uncertain": True}
                    if execution_uncertain
                    else {"execution_started": False}
                ),
            }

        try:
            from agent_host.provider_runtime import runtime

            record = runtime.get_run(str(run.get("run_id") or ""))
            if record is not None and record.task_handle is not None:
                await asyncio.shield(record.task_handle)
                run = record.to_dict()
        except asyncio.CancelledError:
            logger.info(
                "browser interaction branch wait interrupted; provider run remains shielded session=%s branch=%s",
                sid,
                branch.branch_id,
            )
            raise
        except Exception:
            logger.exception("failed waiting for branch provider run")

        lock = self._branch_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            self._update_from_run(run, fallback_session_id=sid, user_text=user_text)
        display_text = self._display_text_for_run(run, branch)
        return {
            "handled": True,
            "branch_id": branch.branch_id,
            "provider": branch.provider,
            "display_text": display_text,
            "speak": bool(display_text),
            "hidden_summary": branch.hidden_summary,
            "visible_messages": list(branch.visible_messages[-8:]),
            "run": run,
        }

    async def _on_provider_result(self, _method: str, params: dict[str, Any]) -> None:
        if not isinstance(params, dict):
            return
        session_id = self._run_session_id(params)
        if not session_id:
            return
        lock = self._branch_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            run_id = str(params.get("run_id") or "").strip()
            status = str(params.get("status") or "").strip().lower()
            if run_id and status in {"done", "error", "cancelled", "canceled"}:
                # A terminal fact releases an earlier stop-uncertainty
                # quarantine even when the run itself is tombstoned. Never let
                # the tombstone suppress liveness recovery.
                self._clear_termination_pending(session_id, run_id)

            if str(params.get("provider") or "").strip().lower() != "browser":
                return
            metadata = (
                params.get("metadata")
                if isinstance(params.get("metadata"), dict)
                else {}
            )
            provider_branch = (
                metadata.get("provider_branch")
                if isinstance(metadata.get("provider_branch"), dict)
                else {}
            )
            declared_branch_id = str(
                metadata.get("interaction_branch_id")
                or provider_branch.get("branch_id")
                or ""
            ).strip()
            branch = self._active_by_session.get(session_id)
            if branch is None:
                # Production registration happens on run.created. A bare
                # terminal result cannot manufacture a new branch after the
                # originating generation has disappeared.
                return
            if declared_branch_id and declared_branch_id != branch.branch_id:
                return
            known_run_ids = {
                str(branch.active_run_id or "").strip(),
                str(branch.last_run_id or "").strip(),
            }
            known_run_ids.discard("")
            if not run_id or run_id not in known_run_ids:
                return
            self._update_from_run(params)

    async def _on_provider_event(self, _method: str, params: dict[str, Any]) -> None:
        """Register browser branches before terminal result and track steer facts."""

        if not isinstance(params, dict):
            return
        if str(params.get("provider") or "").strip().lower() != "browser":
            return
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        event_type = str(params.get("type") or "").strip().lower()
        if event_type == PARENT_CONTEXT_DELIVERED_EVENT:
            return
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        run_id = str(params.get("run_id") or "").strip()
        session_id = str(metadata.get("session_id") or metadata.get("chat_session_id") or "").strip()
        if not session_id and run_id:
            for candidate_session, candidate in self._active_by_session.items():
                if candidate.active_run_id == run_id:
                    session_id = candidate_session
                    break
        if not session_id:
            return
        stale_start: tuple[str, str, str, int, str] | None = None
        replaced_run: tuple[str, str, str, int] | None = None
        lock = self._branch_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            source = str(metadata.get("source") or "").strip().lower()
            is_branch_run = bool(
                metadata.get("provider_branch")
                or metadata.get("interaction_branch_id")
                or source in {"llm_delegate", "browser_branch"}
            )
            branch = self._active_by_session.get(session_id)
            work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
            incoming_work_item_id = str(
                work.get("work_item_id") or work.get("workItemId") or ""
            ).strip()
            incoming_operation_id = str(
                work.get("operation_id") or work.get("operationId") or ""
            ).strip()
            declared_event_branch_id = str(
                metadata.get("interaction_branch_id") or ""
            ).strip()

            if event_type in {"run.finished", "run.failed", "run.cancelled"}:
                # Clear exact stop uncertainty before a semantic tombstone can
                # intentionally suppress this late event's state projection.
                self._clear_termination_pending(session_id, run_id)

            if event_type != "run.created":
                if self._run_was_semantically_closed(run_id):
                    return
                if branch is None:
                    return
                if (
                    declared_event_branch_id
                    and declared_event_branch_id != branch.branch_id
                ):
                    return
                known_run_ids = {
                    str(branch.active_run_id or "").strip(),
                    str(branch.last_run_id or "").strip(),
                }
                known_run_ids.discard("")
                if not run_id or run_id not in known_run_ids:
                    return

            if event_type == "run.created":
                if not run_id:
                    return
                incoming_branch_id = str(metadata.get("interaction_branch_id") or run_id)
                incoming_revision = max(
                    0,
                    _nonnegative_int(
                        metadata.get("branch_instruction_revision")
                    ),
                )
                pending_termination = self.termination_pending_for_session(session_id)
                admitted_scope = (
                    metadata.get("interaction_branch_routing_scope")
                    if isinstance(
                        metadata.get("interaction_branch_routing_scope"),
                        dict,
                    )
                    else None
                )
                admitted_scope_stale = False
                if admitted_scope is not None:
                    admitted_state = str(
                        admitted_scope.get("state") or "bound"
                    ).strip().lower()
                    admitted_sid = str(
                        admitted_scope.get("parent_session_id") or ""
                    ).strip()
                    admitted_scope_stale = bool(
                        admitted_sid != session_id
                        or admitted_state != "absent"
                        or (
                            branch is not None
                            and branch.active_run_id != run_id
                        )
                    )
                active_admission = self.provider_admission_for_session(session_id)
                admission_conflict = bool(
                    active_admission is not None
                    and active_admission.run_id != run_id
                )
                active_run_conflict = bool(
                    branch is not None
                    and str(branch.active_run_id or "").strip()
                    and branch.active_run_id != run_id
                    and branch.branch_id == incoming_branch_id
                )
                stale_generation = bool(
                    branch is not None
                    and branch.branch_id == incoming_branch_id
                    and incoming_revision < branch.instruction_revision
                )
                if (
                    pending_termination
                    or admitted_scope_stale
                    or admission_conflict
                    or active_run_conflict
                ):
                    # No Browser action may start while an earlier exact run in
                    # this Session still has unconfirmed termination. Mark and
                    # cancel this queued record before ProviderRuntime can
                    # schedule its adapter.
                    self._mark_run_semantically_closed(run_id)
                    stale_start = (
                        incoming_branch_id,
                        str(metadata.get("browser_session_id") or ""),
                        run_id,
                        incoming_revision + 1,
                        (
                            "prior_browser_run_stop_unconfirmed"
                            if pending_termination
                            else "turn_start_routing_scope_stale"
                            if admitted_scope_stale
                            else "provider_admission_reserved_by_another_turn"
                            if admission_conflict
                            else "branch_active_run_conflict"
                        ),
                    )
                    self._mark_termination_pending(
                        session_id=session_id,
                        branch_id=incoming_branch_id,
                        run_id=run_id,
                        reason=(
                            "termination_in_progress:prior_browser_run_stop_unconfirmed"
                            if pending_termination
                            else "termination_in_progress:turn_start_routing_scope_stale"
                            if admitted_scope_stale
                            else (
                                "termination_in_progress:"
                                "provider_admission_reserved_by_another_turn"
                            )
                            if admission_conflict
                            else "termination_in_progress:branch_active_run_conflict"
                        ),
                    )
                elif not is_branch_run:
                    return
                elif self._branch_was_semantically_closed(incoming_branch_id) or stale_generation:
                    self._mark_run_semantically_closed(run_id)
                    stale_start = (
                        incoming_branch_id,
                        str(metadata.get("browser_session_id") or ""),
                        run_id,
                        incoming_revision + 1,
                        "stale_branch_run_created",
                    )
                    self._mark_termination_pending(
                        session_id=session_id,
                        branch_id=incoming_branch_id,
                        run_id=run_id,
                        reason="termination_in_progress:stale_branch_run_created",
                    )
                else:
                    if branch is not None and branch.branch_id != incoming_branch_id:
                        replaced_active_run_id = str(branch.active_run_id or "").strip()
                        replaced_browser_session_id = branch.browser_session_id
                        self._close_branch(
                            branch,
                            status="superseded",
                            reason="new_browser_run_created",
                            queue_stop=False,
                        )
                        if replaced_active_run_id:
                            replaced_run = (
                                branch.branch_id,
                                replaced_browser_session_id,
                                replaced_active_run_id,
                                branch.instruction_revision,
                            )
                            self._mark_termination_pending(
                                session_id=session_id,
                                branch_id=branch.branch_id,
                                run_id=replaced_active_run_id,
                                reason="termination_in_progress:new_browser_run_created",
                            )
                        branch = None
                    now = time.time()
                    if branch is None:
                        initial_instruction = str(
                            metadata.get("branch_user_message")
                            or metadata.get("source_user_text")
                            or payload.get("task")
                            or ""
                        ).strip()
                        branch = InteractionBranchState(
                            branch_id=incoming_branch_id,
                            parent_session_id=session_id,
                            provider="browser",
                            status="active",
                            goal=initial_instruction,
                            checkpoint=self._checkpoint_for_session(
                                session_id=session_id,
                                user_intent=initial_instruction,
                                turn_id=str(metadata.get("turn_id") or ""),
                            ),
                            latest_instruction=initial_instruction,
                            created_at=now,
                            work_item_id=incoming_work_item_id,
                            operation_id=incoming_operation_id,
                        )
                    else:
                        if incoming_work_item_id:
                            branch.work_item_id = incoming_work_item_id
                        if incoming_operation_id:
                            branch.operation_id = incoming_operation_id
                    branch.active_run_id = run_id
                    branch.status = "active"
                    branch.instruction_revision = max(
                        branch.instruction_revision,
                        incoming_revision,
                    )
                    branch.accepted_instruction_revision = max(
                        branch.accepted_instruction_revision,
                        incoming_revision,
                    )
                    branch.updated_at = now
                    branch.expires_at = now + self.ttl_seconds
                    branch.metadata = {
                        **branch.metadata,
                        "active_run_id": run_id,
                        "active_run_status": "created",
                    }
                    self._active_by_session[session_id] = branch
                    self._persist(branch)
            elif branch is not None and not (
                branch.active_run_id and branch.active_run_id != run_id
            ):
                browser_session_id = str(payload.get("browser_session_id") or "").strip()
                if browser_session_id:
                    branch.browser_session_id = browser_session_id
                if event_type == "run.status":
                    stage = str(payload.get("stage") or "").strip().lower()
                    if stage == "steer_applied":
                        revision = _nonnegative_int(payload.get("revision"))
                        if revision < branch.accepted_instruction_revision:
                            return
                        branch.applied_instruction_revision = max(
                            branch.applied_instruction_revision,
                            revision,
                        )
                        branch.metadata = {
                            **branch.metadata,
                            "steering": {
                                "state": "applied",
                                "revision": revision,
                                "run_id": run_id,
                            },
                        }
                branch.updated_at = time.time()
                branch.expires_at = branch.updated_at + self.ttl_seconds
                self._persist(branch)
        if stale_start is not None:
            branch_id, browser_session_id, stale_run_id, revision, stale_reason = (
                stale_start
            )
            confirmed, stop_reason, _before_execution = await self._cancel_stale_run_identity(
                session_id=session_id,
                branch_id=branch_id,
                browser_session_id=browser_session_id,
                run_id=stale_run_id,
                revision=revision,
                reason=stale_reason,
            )
            if not confirmed:
                logger.error(
                    "stale browser run could not be stopped before scheduling run=%s reason=%s",
                    stale_run_id,
                    stop_reason,
                )
        if replaced_run is not None:
            old_branch_id, old_browser_session_id, old_run_id, old_revision = replaced_run
            confirmed, stop_reason, _before_execution = await self._cancel_stale_run_identity(
                session_id=session_id,
                branch_id=old_branch_id,
                browser_session_id=old_browser_session_id,
                run_id=old_run_id,
                revision=old_revision,
                reason="new_browser_run_created",
            )
            if not confirmed:
                # Starting the replacement while the old run may still act
                # would create two Browser authorities. Cancel the just-created
                # queued run before this event callback lets Runtime schedule it.
                async with lock:
                    current = self._active_by_session.get(session_id)
                    if current is not None and current.active_run_id == run_id:
                        new_browser_session_id = current.browser_session_id
                        self._close_branch(
                            current,
                            status="superseded",
                            reason="prior_browser_run_stop_unconfirmed",
                            queue_stop=False,
                        )
                    else:
                        new_browser_session_id = str(
                            metadata.get("browser_session_id") or ""
                        )
                    self._mark_run_semantically_closed(run_id)
                new_confirmed, new_stop_reason, _new_before_execution = (
                    await self._cancel_stale_run_identity(
                        session_id=session_id,
                        branch_id=str(
                            metadata.get("interaction_branch_id") or run_id
                        ),
                        browser_session_id=new_browser_session_id,
                        run_id=run_id,
                        revision=max(
                            1,
                            _nonnegative_int(
                                metadata.get("branch_instruction_revision")
                            )
                            + 1,
                        ),
                        reason="prior_browser_run_stop_unconfirmed",
                    )
                )
                logger.error(
                    "browser replacement blocked because prior run stop was unconfirmed "
                    "old_run=%s old_reason=%s new_run=%s new_cancelled=%s new_reason=%s",
                    old_run_id,
                    stop_reason,
                    run_id,
                    new_confirmed,
                    new_stop_reason,
                )

    def _run_session_id(
        self,
        run: Mapping[str, Any],
        *,
        fallback_session_id: str = "",
    ) -> str:
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        browser = metadata.get("browser") if isinstance(metadata.get("browser"), dict) else {}
        session_id = str(
            metadata.get("session_id")
            or browser.get("chat_session_id")
            or fallback_session_id
            or ""
        ).strip()
        run_id = str(run.get("run_id") or "").strip()
        if not session_id and run_id:
            for candidate_session, candidate in self._active_by_session.items():
                if run_id in {candidate.active_run_id, candidate.last_run_id}:
                    return candidate_session
        return session_id

    def _update_from_run(
        self,
        run: dict[str, Any],
        *,
        fallback_session_id: str = "",
        user_text: str = "",
    ) -> InteractionBranchState | None:
        run_id = str(run.get("run_id") or "").strip()
        if run_id and self._run_was_semantically_closed(run_id):
            logger.info("ignore result from semantically closed browser run=%s", run_id)
            return None
        provider = str(run.get("provider") or "").strip().lower()
        if provider != "browser":
            return None
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
        browser = metadata.get("browser") if isinstance(metadata.get("browser"), dict) else {}
        provider_branch = metadata.get("provider_branch") if isinstance(metadata.get("provider_branch"), dict) else {}
        declared_branch_id = str(
            metadata.get("interaction_branch_id")
            or provider_branch.get("branch_id")
            or ""
        ).strip()
        if declared_branch_id and self._branch_was_semantically_closed(declared_branch_id):
            if run_id:
                self._mark_run_semantically_closed(run_id)
            logger.info(
                "ignore result from semantically closed browser branch=%s run=%s",
                declared_branch_id,
                run_id,
            )
            return None
        session_id = str(
            metadata.get("session_id")
            or browser.get("chat_session_id")
            or fallback_session_id
            or ""
        ).strip()
        if not session_id:
            return None

        existing = self._active_by_session.get(session_id)
        browser_session_id = str(
            browser.get("browser_session_id")
            or metadata.get("browser_session_id")
            or (existing.browser_session_id if existing is not None else "")
            or ""
        ).strip()
        status = str(run.get("status") or "").strip().lower()
        if browser.get("closed"):
            if existing is not None:
                self._close_branch(existing, status="closed", reason="browser_closed")
            return None
        if status in {"cancelled", "canceled"}:
            if existing is not None:
                if not browser_session_id:
                    # A run cancelled before adapter execution has no reusable
                    # page context. Keeping it bound would advertise a Browser
                    # authority that never actually existed.
                    self._close_branch(
                        existing,
                        status="cancelled",
                        reason="cancelled_before_browser_session",
                        queue_stop=False,
                    )
                    return None
                now = time.time()
                existing.browser_session_id = browser_session_id
                existing.status = "idle"
                existing.updated_at = now
                existing.expires_at = now + self.ttl_seconds
                existing.last_run_id = str(run.get("run_id") or existing.last_run_id or "")
                if existing.active_run_id == str(run.get("run_id") or ""):
                    existing.active_run_id = ""
                existing.metadata = {
                    **existing.metadata,
                    "last_browser": browser,
                    "last_status": status,
                    "interrupted": True,
                    "interrupted_run_id": str(run.get("run_id") or ""),
                }
                if not existing.pending_goal:
                    existing.pending_goal = self._trim(str(run.get("task") or user_text or existing.goal), 700)
                existing.hidden_summary = self._hidden_summary_for_branch(
                    existing,
                    compact_digest=(
                        "The previous browser branch action was interrupted before completion. "
                        "Keep the browser page/session available and continue the pending user instruction."
                    ),
                    browser=browser,
                )
                self._append_branch_message(
                    existing,
                    role="system",
                    content=existing.hidden_summary,
                    visibility="hidden",
                    source="branch_interrupted",
                    metadata={
                        "run_id": str(run.get("run_id") or ""),
                        "browser_session_id": existing.browser_session_id,
                    },
                )
                logger.info(
                    "preserved browser interaction branch after interrupted provider run session=%s branch=%s",
                    session_id,
                    existing.branch_id,
                )
                self._persist(existing)
                self._publish_hidden_summary(existing)
                return existing
            return None

        if not browser_session_id:
            if existing is not None and status in {"done", "error"}:
                self._close_branch(
                    existing,
                    status=status,
                    reason=f"{status}_before_browser_session",
                    queue_stop=False,
                )
            return None

        actions = provider_branch.get("actions") if isinstance(provider_branch.get("actions"), list) else []
        next_state = provider_branch.get("next_state") if isinstance(provider_branch.get("next_state"), dict) else {}
        title = str(
            browser.get("page_title")
            or browser.get("title")
            or next_state.get("page_title")
            or ""
        ).strip()
        url = str(browser.get("current_url") or next_state.get("current_url") or "").strip()
        if not url:
            urls = browser.get("urls") if isinstance(browser.get("urls"), list) else []
            url = str(urls[-1] if urls else "").strip()

        run_id = str(run.get("run_id") or "")
        if existing is not None and self._should_start_new_branch(
            existing,
            metadata=metadata,
            provider_branch=provider_branch,
            run_id=run_id,
            title=title,
            url=url,
        ):
            self._close_branch(existing, status="superseded", reason="new_browser_semantic_task")
            existing = None
        now = time.time()
        branch = existing or InteractionBranchState(
            branch_id=str(metadata.get("interaction_branch_id") or provider_branch.get("branch_id") or f"ibr_browser_{uuid.uuid4().hex[:10]}"),
            parent_session_id=session_id,
            provider="browser",
            status="active",
            goal=str(run.get("task") or user_text or ""),
            checkpoint=self._checkpoint_for_session(
                session_id=session_id,
                user_intent=str(run.get("task") or user_text or ""),
                turn_id=str(metadata.get("turn_id") or ""),
            ),
            browser_session_id=browser_session_id,
            created_at=now,
        )
        if branch.region_start_index < 0:
            try:
                branch.region_start_index = int(
                    branch.checkpoint.get("region_start_index", -1)
                    if isinstance(branch.checkpoint, dict) else -1
                )
            except Exception:
                branch.region_start_index = -1
        already_recorded = bool(run_id and branch.last_run_id == run_id)
        branch.browser_session_id = browser_session_id
        branch.work_item_id = str(
            work.get("work_item_id")
            or work.get("workItemId")
            or branch.work_item_id
            or ""
        ).strip()
        branch.operation_id = str(
            work.get("operation_id")
            or work.get("operationId")
            or branch.operation_id
            or ""
        ).strip()
        branch.title = title or branch.title
        branch.url = url or branch.url
        branch.page_summary = self._page_fact_summary(title=branch.title, url=branch.url)
        branch.last_result = str(run.get("result") or "")
        branch.last_run_id = str(run.get("run_id") or "")
        if branch.active_run_id == run_id:
            branch.active_run_id = ""
        steering = metadata.get("steering") if isinstance(metadata.get("steering"), dict) else {}
        steering_revision = _nonnegative_int(steering.get("revision"))
        branch.accepted_instruction_revision = max(
            branch.accepted_instruction_revision,
            steering_revision,
        )
        branch.applied_instruction_revision = max(
            branch.applied_instruction_revision,
            steering_revision,
        )
        branch.updated_at = now
        branch.expires_at = now + self.ttl_seconds
        branch.metadata = {
            **branch.metadata,
            "last_provider_branch": provider_branch,
            "last_browser": browser,
            "last_status": status,
        }
        if actions and not already_recorded:
            branch.actions.extend(dict(item) for item in actions if isinstance(item, dict))
            branch.actions = branch.actions[-40:]
        if provider_branch.get("artifacts") and not already_recorded:
            artifacts = provider_branch.get("artifacts")
            if isinstance(artifacts, list):
                branch.artifacts.extend(dict(item) for item in artifacts if isinstance(item, dict))
                branch.artifacts = branch.artifacts[-20:]

        if provider_branch and not actions and status == "done" and self._run_needs_user_value(run, provider_branch):
            branch.status = "waiting_for_user"
            branch.pending_goal = self._pending_goal_from_run(run, branch, user_text=user_text)
        elif status == "error":
            branch.status = "waiting_for_user"
            branch.pending_goal = self._pending_goal_from_run(run, branch, user_text=user_text)
        else:
            branch.status = "idle" if status == "done" else "active"
            branch.pending_goal = ""

        if run.get("task") and not branch.goal:
            branch.goal = str(run.get("task") or "")
        if not already_recorded:
            self._merge_run_into_branch(branch, run, provider_branch=provider_branch, browser=browser)
        self._active_by_session[session_id] = branch
        self._persist(branch)
        self._publish_hidden_summary(branch)
        return branch

    def _close_branch(
        self,
        branch: InteractionBranchState,
        *,
        status: str,
        reason: str,
        queue_stop: bool = True,
    ) -> None:
        branch.instruction_revision += 1
        revision = branch.instruction_revision
        self._mark_branch_semantically_closed(branch.branch_id)
        active_run_id = str(branch.active_run_id or "").strip()
        if active_run_id:
            self._mark_run_semantically_closed(active_run_id)
            if queue_stop:
                self._queue_active_run_stop(
                    branch,
                    run_id=active_run_id,
                    revision=revision,
                    status=status,
                    reason=reason,
                )
            branch.active_run_id = ""
        branch.status = "closed"
        branch.updated_at = time.time()
        branch.metadata = {**branch.metadata, "closed_status": status, "closed_reason": reason}
        self._persist(branch)
        current = self._active_by_session.get(branch.parent_session_id)
        if current is branch or (
            current is not None and current.branch_id == branch.branch_id
        ):
            self._active_by_session.pop(branch.parent_session_id, None)
        # squash-merge：分支区间坍缩为一条 summary 胶囊（用户设计语义：
        # 高分辨率操作区间打标，完成后区间内容以 summary 合并回主对话）
        try:
            self._squash_region_into_main(branch, close_status=status)
        except Exception:
            logger.exception("branch squash-merge failed branch=%s", branch.branch_id)

    def _queue_active_run_stop(
        self,
        branch: InteractionBranchState,
        *,
        run_id: str,
        revision: int,
        status: str,
        reason: str,
    ) -> None:
        if self.provider_steer is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "cannot stop active browser plan outside an event loop run=%s",
                run_id,
            )
            return
        loop.create_task(
            self._stop_active_run(
                branch,
                run_id=run_id,
                revision=revision,
                status=status,
                reason=reason,
            ),
            name=f"browser-branch-stop:{run_id}",
        )

    async def _stop_active_run(
        self,
        branch: InteractionBranchState,
        *,
        run_id: str,
        revision: int,
        status: str,
        reason: str,
    ) -> None:
        if self.provider_steer is None:
            return
        try:
            result = await self.provider_steer(
                {
                    "run_id": run_id,
                    "task": "Stop the remaining browser plan and preserve the browser session.",
                    "revision": revision,
                    "metadata": {
                        "source": "interaction_branch",
                        "session_id": branch.parent_session_id,
                        "interaction_branch_id": branch.branch_id,
                        "branch_control": "supersede" if status == "superseded" else "close",
                        "branch_close_reason": reason,
                        "browser_session_id": branch.browser_session_id,
                        "branch_instruction_revision": revision,
                    },
                }
            )
            if not isinstance(result, dict) or result.get("accepted") is not True:
                logger.info(
                    "active browser plan stop was not accepted run=%s reason=%s",
                    run_id,
                    result.get("reason") if isinstance(result, dict) else "invalid_result",
                )
        except Exception:
            logger.exception("failed to stop active browser plan run=%s", run_id)

    async def _cancel_stale_run(
        self,
        branch: InteractionBranchState,
        *,
        run_id: str,
        revision: int,
        reason: str,
    ) -> tuple[bool, str, bool | None]:
        return await self._cancel_stale_run_identity(
            session_id=branch.parent_session_id,
            branch_id=branch.branch_id,
            browser_session_id=branch.browser_session_id,
            run_id=run_id,
            revision=revision,
            reason=reason,
        )

    async def _cancel_stale_run_identity(
        self,
        *,
        session_id: str,
        branch_id: str,
        browser_session_id: str,
        run_id: str,
        revision: int,
        reason: str,
    ) -> tuple[bool, str, bool | None]:
        """Cancel a run that lost its reserved branch generation."""

        clean_run_id = str(run_id or "").strip()
        if not clean_run_id:
            return True, "no_run_id", True
        self._mark_termination_pending(
            session_id=session_id,
            branch_id=branch_id,
            run_id=clean_run_id,
            reason=f"termination_in_progress:{reason}",
        )

        def unconfirmed(detail: str) -> tuple[bool, str, bool | None]:
            self._mark_termination_pending(
                session_id=session_id,
                branch_id=branch_id,
                run_id=clean_run_id,
                reason=detail,
            )
            return False, detail, None

        cancel_reason = "provider_cancel_unavailable"
        if self.provider_cancel is not None:
            try:
                outcome = await self.provider_cancel(
                    clean_run_id,
                    reason=reason,
                    metadata={
                        "source": "interaction_branch",
                        "session_id": session_id,
                        "interaction_branch_id": branch_id,
                        "browser_session_id": browser_session_id,
                        "branch_instruction_revision": max(1, int(revision)),
                    },
                )
                if isinstance(outcome, Mapping):
                    run = outcome.get("run")
                    run_status = str(
                        run.get("status")
                        if isinstance(run, Mapping)
                        else ""
                    ).strip().lower()
                    cancel_reason = str(
                        outcome.get("reason") or "cancel_unconfirmed"
                    ).strip()
                    if outcome.get("cancelled") is True or run_status in {
                        "done",
                        "error",
                        "cancelled",
                        "canceled",
                    }:
                        before_execution = outcome.get("before_execution")
                        if not isinstance(before_execution, bool):
                            before_execution = self._cancel_was_before_execution(
                                outcome
                            )
                        if run_status in {"done", "error"}:
                            before_execution = False
                        self._clear_termination_pending(session_id, clean_run_id)
                        return (
                            True,
                            cancel_reason or "cancelled",
                            before_execution,
                        )
                else:
                    cancel_reason = "invalid_provider_cancel_receipt"
            except Exception:
                logger.exception("failed to cancel stale browser run=%s", clean_run_id)
                cancel_reason = "provider_cancel_failed"
        if self.provider_steer is None:
            return unconfirmed(cancel_reason)
        try:
            steer_outcome = await self.provider_steer(
                {
                    "run_id": clean_run_id,
                    "task": "Stop the stale browser plan and preserve the browser session.",
                    "revision": max(1, int(revision)),
                    "metadata": {
                        "source": "interaction_branch",
                        "session_id": session_id,
                        "interaction_branch_id": branch_id,
                        "branch_control": "supersede",
                        "branch_close_reason": reason,
                        "browser_session_id": browser_session_id,
                        "branch_instruction_revision": max(1, int(revision)),
                    },
                }
            )
            steer_reason = str(
                steer_outcome.get("reason")
                if isinstance(steer_outcome, Mapping)
                else ""
            ).strip()
            if isinstance(steer_outcome, Mapping) and steer_outcome.get("accepted") is True:
                return unconfirmed(f"{cancel_reason}; stop_steer_accepted")
            return unconfirmed(
                f"{cancel_reason}; {steer_reason or 'stop_steer_unconfirmed'}"
            )
        except Exception:
            logger.exception("failed to stop stale browser run=%s", clean_run_id)
            return unconfirmed(f"{cancel_reason}; stop_steer_failed")

    @staticmethod
    def _cancel_was_before_execution(outcome: Mapping[str, Any]) -> bool | None:
        run = outcome.get("run")
        if not isinstance(run, Mapping):
            return None
        events = run.get("events")
        if not isinstance(events, list):
            return None
        for event in reversed(events):
            if not isinstance(event, Mapping):
                continue
            if str(event.get("type") or "").strip().lower() != "run.cancelled":
                continue
            payload = event.get("payload")
            if isinstance(payload, Mapping) and isinstance(
                payload.get("before_execution"),
                bool,
            ):
                return bool(payload.get("before_execution"))
            return None
        return None

    def _prune_semantic_tombstones(self) -> None:
        now = time.time()
        for registry in (self._closed_run_until, self._closed_branch_until):
            expired = [key for key, expires_at in registry.items() if expires_at <= now]
            for key in expired:
                registry.pop(key, None)

    def _mark_run_semantically_closed(self, run_id: str) -> None:
        clean = str(run_id or "").strip()
        if clean:
            self._closed_run_until[clean] = time.time() + self.ttl_seconds

    def _mark_branch_semantically_closed(self, branch_id: str) -> None:
        clean = str(branch_id or "").strip()
        if clean:
            self._closed_branch_until[clean] = time.time() + self.ttl_seconds

    def _mark_termination_pending(
        self,
        *,
        session_id: str,
        branch_id: str,
        run_id: str,
        reason: str,
    ) -> None:
        sid = str(session_id or "").strip()
        rid = str(run_id or "").strip()
        if not sid or not rid:
            return
        pending = self._termination_pending_by_session.setdefault(sid, {})
        pending[rid] = _PendingBranchTermination(
            branch_id=str(branch_id or ""),
            run_id=rid,
            reason=str(reason or "run_stop_unconfirmed"),
            observed_at=time.time(),
        )

    def _clear_termination_pending(self, session_id: str, run_id: str) -> None:
        sid = str(session_id or "").strip()
        rid = str(run_id or "").strip()
        pending = self._termination_pending_by_session.get(sid)
        if pending is None:
            return
        pending.pop(rid, None)
        if not pending:
            self._termination_pending_by_session.pop(sid, None)

    def termination_pending_for_session(
        self,
        session_id: str,
    ) -> tuple[_PendingBranchTermination, ...]:
        pending = self._termination_pending_by_session.get(
            str(session_id or "").strip(),
            {},
        )
        return tuple(pending.values())

    def _branch_was_semantically_closed(self, branch_id: str) -> bool:
        self._prune_semantic_tombstones()
        return self._closed_branch_until.get(str(branch_id or ""), 0.0) > time.time()

    def _run_was_semantically_closed(self, run_id: str) -> bool:
        self._prune_semantic_tombstones()
        return self._closed_run_until.get(str(run_id or ""), 0.0) > time.time()

    def _squash_region_into_main(self, branch: InteractionBranchState, *, close_status: str) -> None:
        """把主对话中标记为本分支的散落条目坍缩为一条 [BRANCH_SUMMARY] 胶囊。

        对白保留语义（用户设计）：只坍缩**操作性**轮次——即发出了
        branch=continue 标签的轮（指令 + 机械应答）和快通道直达轮，
        它们在写入时被打上 branch_id 标记。以下内容永不坍缩、原样保留：
        - 分支期间的正常语音对白（无标签轮次，包括借分支上下文的闲聊）；
        - 开分支/关分支那两轮对话（branch=new/close 的轮不打标）；
        - observer 的叙述条目。
        胶囊插在第一条被移除条目的位置，保持时间局部性。
        scope guard：会话已切换则跳过。
        """
        from config.settings import BRANCH_SQUASH_MERGE
        from core import session_manager as sm

        if not BRANCH_SQUASH_MERGE:
            return
        if branch.region_start_index < 0:
            return
        if sm.get_current_session_id() != branch.parent_session_id:
            logger.info(
                "skip branch squash: session switched (branch=%s)", branch.branch_id
            )
            return
        dialog = sm.conversation_history.dialog
        start = min(max(0, branch.region_start_index), len(dialog))
        insert_at = -1
        kept: list = dialog[:start]
        removed = 0
        for idx in range(start, len(dialog)):
            entry = dialog[idx]
            if isinstance(entry, dict) and str(entry.get("branch_id") or "") == branch.branch_id:
                if insert_at < 0:
                    insert_at = len(kept)
                removed += 1
                continue
            kept.append(entry)
        capsule = {
            "role": "assistant",
            "content": self._branch_capsule_text(branch, close_status=close_status),
            "branch_capsule": branch.branch_id,
        }
        if insert_at < 0:
            # 区间内没有打标条目（如分支开后立即被 supersede）——仅当
            # 分支确实有过操作时才补一条胶囊，否则完全静默
            if removed == 0 and not branch.visible_messages:
                return
            kept.append(capsule)
        else:
            kept.insert(insert_at, capsule)
        dialog[:] = kept
        try:
            sm.save_session(branch.parent_session_id, enable_conversation=True)
        except Exception:
            logger.exception("failed to persist squashed session %s", branch.parent_session_id)
        logger.info(
            "branch region squashed branch=%s removed=%s capsule_at=%s",
            branch.branch_id,
            removed,
            insert_at if insert_at >= 0 else len(kept) - 1,
        )

    def _branch_capsule_text(self, branch: InteractionBranchState, *, close_status: str) -> str:
        outcome = self._trim(
            branch.hidden_summary or branch.page_summary or branch.last_result, 240
        )
        page = f"{branch.title or 'unknown'} ({branch.url or 'unknown url'})"
        steps = len(branch.actions)
        return (
            f"[BRANCH_SUMMARY] ブラウザ作業（{close_status}）: "
            f"{self._trim(branch.goal or branch.pending_goal, 160) or 'ページ操作'}。"
            f"最終ページ: {self._trim(page, 180)}。"
            f"操作 {steps} 手。結果: {outcome or '記録なし'}"
        )

    def _structural_fast_path(self, branch: InteractionBranchState, text: str) -> tuple[str, str]:
        """三条结构性快通道；其余一律 ignore（交主 LLM 单脑路由）。

        与旧的 11 个关键词启发式不同，这里的每条规则都由"分支状态 +
        消息结构"触发，不依赖任何自然语言词表——因此天然三语、
        不会把普通聊天误吸进分支，也不会漏掉白名单外的表达。
        """
        lowered = text.strip().lower()
        if not lowered:
            return "ignore", "empty_message"

        # 快通道 1：分支在等一个值，且消息形如短值 → 直接 continue
        # （等值场景不该让完整对话轮的延迟挡在中间）
        if (
            branch.status == "waiting_for_user"
            and self._looks_like_short_value(lowered)
            and self._goal_needs_user_value(branch)
        ):
            return "continue", "value_for_waiting_branch"

        # 快通道 2/3：消息含显式 URL → 按域名结构判定
        explicit_site = self._site_key_from_explicit_url(text)
        if explicit_site:
            if explicit_site == self._url_site_key(branch.url):
                return "continue", "explicit_url_same_site"
            return "retarget", "explicit_url_new_site"

        return "ignore", "defer_to_main_llm"

    def _should_start_new_branch(
        self,
        branch: InteractionBranchState,
        *,
        metadata: dict[str, Any],
        provider_branch: dict[str, Any],
        run_id: str,
        title: str,
        url: str,
    ) -> bool:
        incoming_branch_id = str(metadata.get("interaction_branch_id") or provider_branch.get("branch_id") or "").strip()
        if incoming_branch_id and incoming_branch_id == branch.branch_id:
            return False
        if run_id and run_id in {branch.active_run_id, branch.last_run_id}:
            return False
        if incoming_branch_id and incoming_branch_id != branch.branch_id:
            return True
        # 单脑路由：主 LLM 的显式分支意图优先于来源推断。
        # branch_intent=continue 的 run 属于既有分支，绝不 supersede；
        # branch_intent=new 显式开新分支；缺省（旧行为）按 llm_delegate 开新。
        branch_intent = str(metadata.get("branch_intent") or "").strip().lower()
        if branch_intent == "continue":
            return False
        if branch_intent == "new":
            return True
        source = str(metadata.get("source") or "").strip().lower()
        if source == "llm_delegate":
            return True
        if url and self._url_site_key(url) and self._url_site_key(url) != self._url_site_key(branch.url):
            return True
        return False

    def _pending_goal_from_run(self, run: dict[str, Any], branch: InteractionBranchState, *, user_text: str = "") -> str:
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        candidate = str(metadata.get("branch_user_message") or user_text or branch.pending_goal or "").strip()
        if not candidate:
            task = str(run.get("task") or "").strip()
            if not self._looks_like_generated_branch_task(task):
                candidate = task
        return self._trim(candidate or branch.goal, 700)

    @staticmethod
    def _run_needs_user_value(run: dict[str, Any], provider_branch: dict[str, Any]) -> bool:
        text = " ".join(
            str(value or "")
            for value in (
                run.get("task"),
                run.get("result"),
                provider_branch.get("final_report"),
                provider_branch.get("compact_digest"),
                provider_branch.get("reason"),
            )
        ).lower()
        need_markers = (
            "need",
            "missing",
            "ask",
            "waiting",
            "keyword",
            "query",
            "search term",
            "\u9700\u8981",
            "\u7f3a",
            "\u7b49\u5f85",
            "\u5173\u952e\u8bcd",
            "\u641c\u7d22\u8bcd",
            "\u691c\u7d22\u30ef\u30fc\u30c9",
            "\u30ad\u30fc\u30ef\u30fc\u30c9",
            "\u5fc5\u8981",
        )
        return any(marker in text for marker in need_markers) and any(
            token in text for token in ("search", "\u641c", "\u691c\u7d22", "query", "keyword")
        )

    @classmethod
    def _site_key_from_explicit_url(cls, text: str) -> str:
        match = re.search(r"https?://[^\s)>\]}]+", str(text or ""), re.I)
        if not match:
            return ""
        return cls._url_site_key(match.group(0))

    @classmethod
    def _url_site_key(cls, url: str) -> str:
        host = ""
        try:
            host = urlparse(str(url or "")).hostname or ""
        except Exception:
            host = ""
        host = host.lower()
        if not host:
            return ""
        if "bilibili.com" in host:
            return "bilibili"
        if "wikipedia.org" in host:
            return "wikipedia"
        if "github.com" in host:
            return "github"
        if "youtube.com" in host or "youtu.be" in host:
            return "youtube"
        if "google." in host:
            return "google"
        if "duckduckgo.com" in host:
            return "duckduckgo"
        if "zenn.dev" in host:
            return "zenn"
        if "qiita.com" in host:
            return "qiita"
        parts = host.split(".")
        return parts[-2] if len(parts) >= 2 else host

    @staticmethod
    def _looks_like_short_value(text: str) -> bool:
        compact = text.strip().strip(".?!,;: \t\r\n")
        if not compact:
            return False
        if len(compact) > 64:
            return False
        if re.search(r"\s", compact) and len(compact.split()) > 4:
            return False
        return True

    @staticmethod
    def _goal_needs_user_value(branch: InteractionBranchState) -> bool:
        goal = f"{branch.goal} {branch.pending_goal}".lower()
        return any(token in goal for token in ("search", "\u641c", "\u691c\u7d22", "query", "keyword"))

    @staticmethod
    def _branch_task(branch: InteractionBranchState, user_text: str) -> str:
        parts = [
            "Continue the active browser interaction branch.",
            f"Latest user instruction: {user_text}",
            "The latest user instruction is authoritative. If it conflicts with older branch history, follow the latest instruction.",
        ]
        if branch.title or branch.url:
            parts.append(f"Current page: {branch.title or 'unknown'} {branch.url or ''}.")
        parts.append(f"Branch goal: {branch.goal or 'continue current page interaction'}.")
        checkpoint_messages = branch.checkpoint.get("recent_messages") if isinstance(branch.checkpoint, dict) else []
        if checkpoint_messages:
            parts.append("Main chat checkpoint:")
            for item in InteractionBranchCoordinator._clean_checkpoint_messages(checkpoint_messages, limit=4):
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "")
                content = str(item.get("content") or "").strip()
                if role and content:
                    parts.append(f"- {role}: {InteractionBranchCoordinator._trim(content, 240)}")
        transcript = InteractionBranchCoordinator._branch_transcript_messages(branch.visible_messages, limit=8)
        if transcript:
            parts.append("Branch user transcript:")
            for item in transcript:
                parts.append(
                    f"- {item.get('role')}: {InteractionBranchCoordinator._trim(str(item.get('content') or ''), 240)}"
                )
        if branch.pending_goal:
            parts.append(f"Pending branch goal: {branch.pending_goal}.")
        if branch.hidden_summary:
            parts.append(f"Branch state summary: {InteractionBranchCoordinator._trim(branch.hidden_summary, 500)}")
        parts.append(
            "Use the current page DOM and interaction refs to choose precise actions. "
            "If the user supplied a query or value, apply it to the relevant page control."
        )
        return "\n".join(parts)

    def _display_text_for_run(self, run: dict[str, Any], branch: InteractionBranchState) -> str:
        run_id = str(run.get("run_id") or "").strip()
        if branch.visible_summary and (not run_id or branch.last_run_id == run_id):
            return branch.visible_summary
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        provider_branch = metadata.get("provider_branch") if isinstance(metadata.get("provider_branch"), dict) else {}
        decision = self._outcome_verdict_for_run(
            branch,
            run,
            provider_branch=provider_branch,
        )
        return decision.summary

    def _merge_run_into_branch(
        self,
        branch: InteractionBranchState,
        run: dict[str, Any],
        *,
        provider_branch: dict[str, Any],
        browser: dict[str, Any],
    ) -> None:
        final_report = str(provider_branch.get("final_report") or run.get("result") or "").strip()
        compact_digest = str(provider_branch.get("compact_digest") or final_report or "").strip()
        if final_report:
            self._append_branch_message(
                branch,
                role="assistant",
                content=self._trim(final_report, 900),
                visibility="hidden",
                source="provider_merge",
                metadata={
                    "run_id": run.get("run_id") or "",
                    "status": run.get("status") or "",
                    "content_type": "provider_report",
                },
            )
        decision = self._outcome_verdict_for_run(
            branch,
            run,
            provider_branch=provider_branch,
        )
        branch.visible_summary = self._trim(decision.summary, 520)
        branch.completeness = decision.completeness
        branch.attention = decision.attention
        branch.completion_rationale = decision.rationale
        hidden = self._hidden_summary_for_branch(branch, compact_digest=compact_digest, browser=browser)
        branch.hidden_summary = hidden
        branch.metadata = {
            **branch.metadata,
            "outcome_verdict": decision.to_dict(),
        }
        branch.merge_count += 1
        self._append_branch_message(
            branch,
            role="system",
            content=hidden,
            visibility="hidden",
            source="branch_hidden_merge",
            metadata={
                "run_id": run.get("run_id") or "",
                "browser_session_id": branch.browser_session_id,
                "url": branch.url,
                "title": branch.title,
                "attention": branch.attention,
                "completeness": branch.completeness,
            },
        )

    def _outcome_verdict_for_run(
        self,
        branch: InteractionBranchState,
        run: dict[str, Any],
        *,
        provider_branch: dict[str, Any],
    ) -> ProviderOutcomeVerdict:
        metadata = (
            dict(run.get("metadata"))
            if isinstance(run.get("metadata"), dict)
            else {}
        )
        raw_evidence = metadata.get(OUTCOME_EVIDENCE_METADATA_KEY)
        if isinstance(raw_evidence, dict) and branch.status == "waiting_for_user":
            metadata[OUTCOME_EVIDENCE_METADATA_KEY] = {
                **raw_evidence,
                "pending_input": True,
            }
        provider_report = str(
            provider_branch.get("final_report") or run.get("result") or ""
        ).strip()
        verdict = assess_provider_outcome(
            execution_status=str(run.get("status") or "failed"),
            provider_report=provider_report,
            metadata=metadata,
            display_language=self._display_language(),
        )
        if verdict is not None:
            return verdict
        # Old or failed Browser attempts can predate the outcome contract.
        # This fail-closed record carries only live host observations and no
        # expected state, so it can render an honest page fact but can never
        # certify provider prose.
        metadata[OUTCOME_EVIDENCE_METADATA_KEY] = ProviderOutcomeEvidence(
            facet="browser.page_state",
            operation="legacy",
            expected={},
            observed={"title": branch.title, "url": branch.url},
            pending_input=branch.status == "waiting_for_user",
        ).to_dict()
        fallback = assess_provider_outcome(
            execution_status=str(run.get("status") or "failed"),
            provider_report=provider_report,
            metadata=metadata,
            display_language=self._display_language(),
        )
        assert fallback is not None
        return fallback

    def _display_language(self) -> str:
        if self._get_display_language is None:
            return "english"
        try:
            return str(self._get_display_language() or "english")
        except Exception:
            logger.exception("failed to read interaction branch display language")
            return "english"

    @staticmethod
    def _page_fact_summary(*, title: str, url: str) -> str:
        clean_title = InteractionBranchCoordinator._trim(title, 180)
        clean_url = InteractionBranchCoordinator._trim(url, 800)
        if clean_title and clean_url:
            return f"{clean_title} — {clean_url}"
        return clean_title or clean_url

    def _hidden_summary_for_branch(
        self,
        branch: InteractionBranchState,
        *,
        compact_digest: str,
        browser: dict[str, Any],
    ) -> str:
        action_summary = self._action_summary(branch.actions[-8:])
        parts = [
            f"Browser conversation branch {branch.branch_id} is active.",
            f"browser_session_id={branch.browser_session_id}",
            f"title={branch.title or browser.get('title') or 'unknown'}",
            f"url={branch.url or browser.get('current_url') or 'unknown'}",
        ]
        if branch.goal:
            parts.append(f"original_goal={self._trim(branch.goal, 240)}")
        if compact_digest:
            parts.append(f"latest_result={self._trim(compact_digest, 360)}")
        if action_summary:
            parts.append(f"recent_actions={action_summary}")
        if branch.pending_goal:
            parts.append(f"pending_goal={self._trim(branch.pending_goal, 240)}")
        if branch.completion_rationale:
            parts.append(
                "terminal_assessment="
                f"{branch.completeness}/{branch.attention}: "
                f"{self._trim(branch.completion_rationale, 300)}"
            )
        parts.append("Continue follow-up browser/page operations inside this branch; do not expose raw DOM to main chat.")
        return "\n".join(parts)

    @staticmethod
    def _action_summary(actions: list[dict[str, Any]]) -> str:
        labels: list[str] = []
        for item in actions:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip()
            ref = str(item.get("ref") or item.get("url") or "").strip()
            if action:
                labels.append(f"{action}{'(' + ref + ')' if ref else ''}")
        return ", ".join(labels[-8:])

    def _append_branch_message(
        self,
        branch: InteractionBranchState,
        *,
        role: str,
        content: str,
        visibility: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        text = str(content or "").strip()
        if not text:
            return
        item = {
            "role": str(role or "system"),
            "content": text,
            "visibility": "hidden" if visibility == "hidden" else "visible",
            "source": str(source or "branch"),
            "created_at": time.time(),
            "metadata": dict(metadata or {}),
        }
        if item["visibility"] == "hidden":
            branch.hidden_messages.append(item)
            branch.hidden_messages = branch.hidden_messages[-60:]
        else:
            branch.visible_messages.append(item)
            branch.visible_messages = branch.visible_messages[-40:]

    @staticmethod
    def _checkpoint_for_session(*, session_id: str, user_intent: str, turn_id: str = "") -> dict[str, Any]:
        """分支入口快照：继承主对话历史 + 记录 squash 区间起点。

        用户设计语义：分支拥有此前闲聊历史（帮助分支执行层理解上文），
        高分辨率操作从此处开始打标，关闭时区间坍缩为 summary 回主对话。
        """
        recent_messages: list[dict[str, str]] = []
        region_start_index = -1
        try:
            from config.settings import BRANCH_CHECKPOINT_MESSAGES
            from core import session_manager as sm

            window = max(4, int(BRANCH_CHECKPOINT_MESSAGES))
            if session_id and sm.get_current_session_id() != session_id:
                # The loaded in-memory chat may belong to a different UI
                # session. Keep the checkpoint empty rather than copying the
                # wrong conversation.
                recent_messages = []
            else:
                region_start_index = len(sm.conversation_history.dialog)
                for message in list(sm.conversation_history.dialog)[-window:]:
                    if not isinstance(message, dict):
                        continue
                    role = str(message.get("role") or "")
                    content = str(message.get("content") or "")
                    if InteractionBranchCoordinator._looks_like_checkpoint_noise(content):
                        continue
                    if role in {"user", "assistant"} and content:
                        recent_messages.append({"role": role, "content": content[:900]})
        except Exception:
            logger.exception("failed to capture interaction branch checkpoint")
        return {
            "parent_session_id": str(session_id or ""),
            "parent_turn_id": str(turn_id or ""),
            "user_intent": str(user_intent or ""),
            "recent_messages": recent_messages,
            "region_start_index": region_start_index,
            "created_at": time.time(),
        }

    def _publish_hidden_summary(self, branch: InteractionBranchState) -> None:
        if not branch.hidden_summary:
            return
        try:
            from server.work_context import add_work_note

            add_work_note(
                {
                    "source": "interaction_branch",
                    "provider": branch.provider,
                    "run_id": branch.last_run_id,
                    "session_id": branch.parent_session_id,
                    "phase": "Branch",
                    "title": f"{branch.provider.title()} conversation branch",
                    "summary": self._trim(branch.hidden_summary, 420),
                    "importance": "normal",
                    "observer_policy": "silent",
                    "metadata": {
                        "continuable": True,
                        "provider_context_kind": "conversation_branch",
                        "interaction_branch_id": branch.branch_id,
                        "browser_session_id": branch.browser_session_id,
                        "url": branch.url,
                        "page_title": branch.title,
                        "completion": branch.completeness,
                        "attention": branch.attention,
                        "completion_rationale": self._trim(
                            branch.completion_rationale,
                            500,
                        ),
                        "hidden_summary": self._trim(branch.hidden_summary, 900),
                    },
                }
            )
        except Exception:
            logger.exception("failed to publish interaction branch work context")

    def _is_expired(self, branch: InteractionBranchState) -> bool:
        # A live Provider run owns its liveness/cancellation lifecycle. Expiring
        # the conversation projection synchronously while that run is active
        # could let a new route overlap it before exact cancellation completes.
        return bool(
            not branch.active_run_id
            and branch.expires_at
            and time.time() > branch.expires_at
        )

    def _persist(self, branch: InteractionBranchState) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            storage_key = hashlib.sha256(
                str(branch.branch_id or "").encode("utf-8", errors="replace")
            ).hexdigest()
            path = self.root / f"branch_{storage_key}.json"
            path.write_text(json.dumps(asdict(branch), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("failed to persist interaction branch %s", branch.branch_id)

    @staticmethod
    def _trim(text: str, limit: int) -> str:
        cleaned = " ".join(str(text or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)].rstrip() + "..."

    @staticmethod
    def _looks_like_generated_branch_task(text: str) -> bool:
        cleaned = str(text or "").strip()
        return cleaned.startswith("Continue the active browser interaction branch.")

    @staticmethod
    def _looks_like_checkpoint_noise(text: str) -> bool:
        cleaned = str(text or "").strip()
        return bool(
            cleaned.startswith("[WORK_OBSERVER]")
            or cleaned.startswith("### Browser result")
            or cleaned.startswith("Browser result")
        )

    @staticmethod
    def _clean_checkpoint_messages(raw_messages: Any, *, limit: int) -> list[dict[str, Any]]:
        if not isinstance(raw_messages, list):
            return []
        result: list[dict[str, Any]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content or InteractionBranchCoordinator._looks_like_checkpoint_noise(content):
                continue
            result.append(item)
        return result[-max(1, limit):]

    @staticmethod
    def _branch_transcript_messages(raw_messages: Any, *, limit: int) -> list[dict[str, Any]]:
        if not isinstance(raw_messages, list):
            return []
        result: list[dict[str, Any]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "")
            visibility = str(item.get("visibility") or "visible")
            content = str(item.get("content") or "").strip()
            if visibility == "hidden" or not content:
                continue
            if source in {"provider_merge", "branch_hidden_merge", "branch_interrupted"}:
                continue
            if InteractionBranchCoordinator._looks_like_checkpoint_noise(content):
                continue
            result.append(item)
        return result[-max(1, limit):]


# 模块级单例（configure() 时注册；bootstrap 创建的实例即当前协调器）
_current_coordinator: InteractionBranchCoordinator | None = None


def get_interaction_branch_coordinator() -> InteractionBranchCoordinator | None:
    return _current_coordinator


def capture_interaction_branch_routing_scope(session_id: str) -> dict[str, Any]:
    """Freeze either the exact branch generation or its explicit absence."""

    sid = str(session_id or "").strip()
    if not sid:
        return {
            "state": "invalid",
            "parent_session_id": "",
            "reason": "routing_scope_session_unavailable",
            "captured_at": time.time(),
        }
    coordinator = get_interaction_branch_coordinator()
    if coordinator is None:
        return {
            "state": "invalid",
            "parent_session_id": sid,
            "reason": "interaction_coordinator_unavailable",
            "captured_at": time.time(),
        }
    pending = coordinator.termination_pending_for_session(sid)
    if pending:
        first = pending[0]
        return {
            "state": "quarantined",
            "parent_session_id": sid,
            "branch_id": first.branch_id,
            "run_id": first.run_id,
            "reason": first.reason,
            "pending_run_count": len(pending),
            "captured_at": time.time(),
        }
    reservation = coordinator.provider_admission_for_session(sid)
    if reservation is not None:
        return {
            "state": "reserved",
            "parent_session_id": sid,
            "reason": "provider_admission_in_progress",
            "provider": reservation.provider,
            "captured_at": time.time(),
        }
    lease = coordinator.capture_routing_lease(sid)
    if lease is not None:
        return {"state": "bound", **lease.as_dict()}
    return {
        "state": "absent",
        "parent_session_id": sid,
        "captured_at": time.time(),
    }
