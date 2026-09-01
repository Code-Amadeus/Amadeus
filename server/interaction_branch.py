"""Conversation-level interaction branch coordinator.

This module is intentionally above ProviderRuntime and below main chat.

ProviderRuntime executes work. BrowserAdapter owns Playwright sessions.
InteractionBranchCoordinator owns the short-lived conversation state that lets
the next user turn continue inside a browser branch instead of forcing the main
chat to guess from a thin provider handle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from server.event_bus import bus
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


ProviderRunCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ProviderSteerCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
    applied_instruction_revision: int = 0
    latest_instruction: str = ""
    # squash-merge 区间起点：分支创建时主对话 dialog 的长度。
    # 关闭时从此索引起扫描带本分支 branch_id 标记的条目做坍缩。
    region_start_index: int = -1
    metadata: dict[str, Any] = field(default_factory=dict)


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
        root: str | Path = Path("runtime") / "interaction_branches",
        ttl_seconds: float = 900.0,
        display_language: Callable[[], str] | None = None,
    ) -> None:
        self.provider_run = provider_run
        self.provider_steer = provider_steer
        self.root = Path(root)
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self._get_display_language = display_language
        self._active_by_session: dict[str, InteractionBranchState] = {}
        self._branch_locks: dict[str, asyncio.Lock] = {}
        self._closed_run_until: dict[str, float] = {}
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

    def close_active_branch(self, session_id: str, *, reason: str = "llm_close") -> bool:
        """主 LLM 发出 branch=close：关闭当前会话的活跃分支。"""
        branch = self.active_branch_for_session(session_id)
        if branch is None:
            return False
        self._close_branch(branch, status="closed", reason=reason)
        return True

    def close_for_provider_handoff(
        self,
        session_id: str,
        *,
        next_provider: str,
    ) -> bool:
        """Retire a live branch when canonical control selects another Provider.

        An interaction branch is an execution context, not the Session's
        routing authority. Keeping it active after a Provider handoff lets its
        structural fast paths steal later turns from the newly selected context.
        Same-provider work remains on the normal continue/new/close lifecycle.
        """

        branch = self.active_branch_for_session(session_id)
        selected = str(next_provider or "").strip().lower()
        if branch is None or not selected or branch.provider == selected:
            return False
        self._close_branch(
            branch,
            status="superseded",
            reason=f"provider_handoff:{selected}",
        )
        return True

    async def continue_from_delegate(
        self,
        *,
        session_id: str,
        task: str,
        source_user_text: str = "",
        turn_id: str = "",
    ) -> dict[str, Any] | None:
        """主 LLM 发出 branch=continue：在活跃分支内后台执行规范化指令。

        与旧的同步路由不同：不 await run 完成——发标签的那轮对话已经给了
        用户即时应答。分支终态由 PROVIDER_RESULT 订阅写回；Browser snapshot
        仍保持静音，后续读取只能使用经过宿主事实约束的 visible_summary。
        返回 run 信息；无活跃分支时返回 None（调用方按 branch=new 处理）。
        """
        branch = self.active_branch_for_session(session_id)
        if branch is None or branch.provider != "browser" or not (
            branch.browser_session_id or branch.active_run_id
        ):
            return None
        # The model-authored task is an execution proposal.  The exact source
        # turn owns the branch goal and visible transcript; otherwise one bad
        # paraphrase becomes durable "user" context and is amplified by every
        # later continuation.
        user_text = str(source_user_text or task or "").strip()
        if not user_text:
            return None
        self._append_branch_message(
            branch,
            role="user",
            content=user_text,
            visibility="visible",
            source=(
                "main_chat_intervention"
                if str(source_user_text or "").strip()
                else "legacy_llm_branch_continue"
            ),
            metadata={"turn_id": turn_id},
        )
        branch.status = "active"
        if not branch.pending_goal:
            branch.pending_goal = self._trim(user_text, 700)
        branch.updated_at = time.time()
        branch.expires_at = branch.updated_at + self.ttl_seconds
        self._persist(branch)
        logger.info(
            "llm-routed branch continuation session=%s branch=%s task=%r",
            session_id,
            branch.branch_id,
            user_text[:60],
        )
        return await self._start_or_steer(
            branch,
            user_text,
            turn_id=turn_id,
            route_reason="llm_branch_continue",
        )

    async def _start_or_steer(
        self,
        branch: InteractionBranchState,
        user_text: str,
        *,
        turn_id: str,
        route_reason: str,
    ) -> dict[str, Any]:
        """Serialize one branch and reuse its active run when steerable."""

        lock = self._branch_locks.setdefault(branch.parent_session_id, asyncio.Lock())
        async with lock:
            branch.instruction_revision += 1
            branch.latest_instruction = self._trim(user_text, 700)
            revision = branch.instruction_revision
            params = self._build_continue_params(
                branch,
                user_text,
                turn_id=turn_id,
                route_reason=route_reason,
            )
            if branch.active_run_id and self.provider_steer is not None:
                steer_result = await self.provider_steer(
                    {
                        "run_id": branch.active_run_id,
                        "task": params["task"],
                        "revision": revision,
                        "metadata": dict(params["metadata"]),
                    }
                )
                if isinstance(steer_result, dict) and steer_result.get("accepted") is True:
                    branch.metadata = {
                        **branch.metadata,
                        "steering": {
                            "state": "queued",
                            "revision": revision,
                            "run_id": branch.active_run_id,
                            "turn_id": turn_id,
                        },
                    }
                    branch.updated_at = time.time()
                    self._persist(branch)
                    run = steer_result.get("run")
                    return dict(run) if isinstance(run, dict) else {
                        "run_id": branch.active_run_id,
                        "provider": branch.provider,
                        "status": "running",
                    }

                reason = str(
                    steer_result.get("reason") if isinstance(steer_result, dict) else ""
                )
                run = steer_result.get("run") if isinstance(steer_result, dict) else {}
                run_status = str(run.get("status") if isinstance(run, dict) else "").lower()
                if reason not in {"already_finished", "not_found"} and run_status in {
                    "queued",
                    "running",
                }:
                    # Never start a second run against the same Playwright
                    # session. A rejection this late is an audit-visible
                    # next-turn deferral; the caller may retry once terminal.
                    branch.metadata = {
                        **branch.metadata,
                        "steering": {
                            "state": "deferred",
                            "revision": revision,
                            "run_id": branch.active_run_id,
                            "reason": reason or "active_run_rejected_steer",
                        },
                    }
                    self._persist(branch)
                    return dict(run)
                branch.active_run_id = ""

            response = await self.provider_run(params)
            run = response.get("run") if isinstance(response, dict) else {}
            if not isinstance(run, dict):
                run = {}
            branch.active_run_id = str(run.get("run_id") or "")
            branch.status = "active"
            branch.updated_at = time.time()
            self._persist(branch)
            return run

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
    ) -> dict[str, Any] | None:
        """Route a user turn into the active branch when it is a continuation."""

        user_text = str(text or "").strip()
        sid = str(session_id or "").strip()
        if not user_text or not sid:
            return None
        branch = self.active_branch_for_session(sid)
        if branch is None:
            return None
        if branch.provider != "browser" or not (
            branch.browser_session_id or branch.active_run_id
        ):
            return None
        # 三条结构性快通道（按分支状态/显式结构触发，不查词表）。
        # 其余一切消息返回 None → 落回主对话，由主 LLM 借助分支状态块
        # 决定 branch=continue/new/close（单脑路由）。
        route_kind, route_reason = self._structural_fast_path(branch, user_text)
        if route_kind == "retarget":
            self._close_branch(branch, status="superseded", reason=route_reason)
            return None
        if route_kind != "continue":
            return None

        self._append_branch_message(
            branch,
            role="user",
            content=user_text,
            visibility="visible",
            source="branch_followup",
            metadata={"turn_id": turn_id},
        )
        branch.status = "active"
        if not branch.pending_goal:
            branch.pending_goal = self._trim(user_text, 700)
        branch.updated_at = time.time()
        branch.expires_at = branch.updated_at + self.ttl_seconds
        self._persist(branch)
        logger.info(
            "routing chat turn into browser interaction branch session=%s branch=%s",
            sid,
            branch.branch_id,
        )
        run = await self._start_or_steer(
            branch,
            user_text,
            turn_id=turn_id,
            route_reason=route_reason,
        )

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
        if isinstance(params, dict):
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

        if event_type == "run.created":
            if not is_branch_run or not run_id:
                return
            incoming_branch_id = str(metadata.get("interaction_branch_id") or run_id)
            if branch is not None and branch.branch_id != incoming_branch_id:
                self._close_branch(
                    branch,
                    status="superseded",
                    reason="new_browser_run_created",
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
                int(metadata.get("branch_instruction_revision") or 0),
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
            return

        if branch is None or (branch.active_run_id and branch.active_run_id != run_id):
            return
        browser_session_id = str(payload.get("browser_session_id") or "").strip()
        if browser_session_id:
            branch.browser_session_id = browser_session_id
        if event_type == "run.status":
            stage = str(payload.get("stage") or "").strip().lower()
            if stage == "steer_applied":
                revision = max(0, int(payload.get("revision") or 0))
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
        browser_session_id = str(browser.get("browser_session_id") or metadata.get("browser_session_id") or "").strip()
        if not browser_session_id:
            return None
        session_id = str(
            metadata.get("session_id")
            or browser.get("chat_session_id")
            or fallback_session_id
            or ""
        ).strip()
        if not session_id:
            return None

        status = str(run.get("status") or "").strip().lower()
        if browser.get("closed"):
            existing = self._active_by_session.get(session_id)
            if existing is not None:
                self._close_branch(existing, status="closed", reason="browser_closed")
            return None
        if status in {"cancelled", "canceled"}:
            existing = self._active_by_session.get(session_id)
            if existing is not None:
                now = time.time()
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

        provider_branch = metadata.get("provider_branch") if isinstance(metadata.get("provider_branch"), dict) else {}
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

        existing = self._active_by_session.get(session_id)
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
        branch.applied_instruction_revision = max(
            branch.applied_instruction_revision,
            int(steering.get("revision") or 0),
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

    def _close_branch(self, branch: InteractionBranchState, *, status: str, reason: str) -> None:
        active_run_id = str(branch.active_run_id or "").strip()
        if active_run_id:
            branch.instruction_revision += 1
            revision = branch.instruction_revision
            self._closed_run_until[active_run_id] = time.time() + self.ttl_seconds
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

    def _run_was_semantically_closed(self, run_id: str) -> bool:
        now = time.time()
        expired = [
            key for key, expires_at in self._closed_run_until.items()
            if expires_at <= now
        ]
        for key in expired:
            self._closed_run_until.pop(key, None)
        return self._closed_run_until.get(str(run_id or ""), 0.0) > now

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
        if run_id and run_id == branch.last_run_id:
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
        return bool(branch.expires_at and time.time() > branch.expires_at)

    def _persist(self, branch: InteractionBranchState) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{branch.branch_id}.json"
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
