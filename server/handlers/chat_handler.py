"""Adapter for LLM chat pipeline – wraps stream_llm_query from main.py."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from config.settings import PENDING_TURN_GATE_TIMEOUT_S
from server.desktop_voice import is_manual_wake_command
from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class ChatHandler(RequestHandler):
    methods = [Method.CHAT_SEND, Method.CHAT_ABORT]

    def __init__(self) -> None:
        self._stream_task: asyncio.Task | None = None
        self._stream_llm_query = None          # injected by configure()
        self._pending_sentence_items = None
        self._on_turn_finished = None
        self._interaction_branch_router = None
        self._assistant_voice_sink = None
        self._presentation_interrupt = None
        self._background_interaction_interrupt = None
        self._manual_wake_handler = None
        self._manual_wake_phrases = ""
        self._chat_epoch = 0
        self._active_turn_id = ""
        self._active_accumulated_text = ""
        self._last_assistant_turn_id = ""
        self._last_assistant_text = ""

    def configure(
        self,
        stream_llm_query,
        pending_sentence_items,
        on_turn_finished=None,
        interaction_branch_router=None,
        assistant_voice_sink=None,
        presentation_interrupt=None,
        background_interaction_interrupt=None,
        manual_wake_handler=None,
        manual_wake_phrases="",
    ) -> None:
        self._stream_llm_query = stream_llm_query
        self._pending_sentence_items = pending_sentence_items
        self._on_turn_finished = on_turn_finished
        self._interaction_branch_router = interaction_branch_router
        self._assistant_voice_sink = assistant_voice_sink
        self._presentation_interrupt = presentation_interrupt
        self._background_interaction_interrupt = background_interaction_interrupt
        self._manual_wake_handler = manual_wake_handler
        self._manual_wake_phrases = manual_wake_phrases

    def is_busy(self) -> bool:
        return bool(self._active_turn_id or (self._stream_task is not None and not self._stream_task.done()))

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.CHAT_SEND:
            return await self._handle_send(params)
        if method == Method.CHAT_ABORT:
            return await self._handle_abort(params)
        return None

    async def send_text(
        self,
        text: str,
        *,
        provider: Any = None,
        session_id: str = "",
        turn_id: str = "",
        source: str = "",
        pending: bool = False,
    ) -> dict[str, Any]:
        return await self._handle_send(
            {
                "text": text,
                "provider": provider,
                "session_id": session_id,
                "turn_id": turn_id or uuid.uuid4().hex,
                "source": source,
                "pending": pending,
            }
        )

    async def _handle_send(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._stream_llm_query is None:
            raise RuntimeError("chat handler not configured")

        text = params.get("text", "")
        if self._manual_wake_handler and is_manual_wake_command(
            text,
            self._manual_wake_phrases,
        ):
            result = self._manual_wake_handler(
                {"text": str(text), "source": "manual_text"}
            )
            if hasattr(result, "__await__"):
                await result
            return {"status": "awake", "control": "wake"}
        turn_id = params.get("turn_id", "")
        provider = params.get("provider", None)
        visual_request = params.get("visual", None)
        pending = bool(params.get("pending", False))
        session_id = str(params.get("session_id") or "")
        if not session_id:
            try:
                os.environ.setdefault("AMADEUS_HEADLESS", "1")
                from core import session_manager as sm

                session_id = sm.get_current_session_id() or ""
            except Exception:
                session_id = ""
        if session_id:
            try:
                os.environ.setdefault("AMADEUS_HEADLESS", "1")
                from core import session_manager as sm
                from core.chat_runtime import get_chat_runtime
                sm.set_current_session_id(session_id)
                get_chat_runtime().enable_conversation = True
            except Exception:
                logger.exception("failed to bind chat turn to session %s", session_id)
        # A new confirmed user turn supersedes an unfinished role turn.  Use
        # the existing compound interrupt owner so generation, queued speech,
        # playback and history annotation close as one boundary before a new
        # epoch is issued.  Speculative pending turns keep their established
        # gate semantics and are resolved by SpeculativeTurnLauncher instead.
        if (
            not pending
            and self._active_turn_id
            and self._active_turn_id != turn_id
        ):
            await self._interrupt_superseded_turn()
        elif not pending:
            await self._interrupt_background_presentation()
        await self._interrupt_background_interaction()
        loop = asyncio.get_running_loop()
        # 向账本申领轮次：epoch 发放 + 身份登记 + 重叠校验一次原子完成
        # （所有权迁移·切片 C；账本不可用时 _open_turn 内部回退本地自增）
        # pending=True 开投机轮：LLM 照常流式，TTS 条目在出队点被扣住待决议
        grant = self._open_turn(
            turn_id=turn_id,
            session_id=session_id,
            source=str(params.get("source") or ""),
            pending=bool(params.get("pending", False)),
        )
        self._chat_epoch = int(grant["chat_epoch"])
        chat_epoch = self._chat_epoch
        self._active_turn_id = turn_id
        self._active_accumulated_text = ""

        def token_callback(accumulated: str) -> None:
            if chat_epoch != self._chat_epoch or turn_id != self._active_turn_id:
                return
            if pending:
                return
            self._active_accumulated_text = str(accumulated or "")
            loop.create_task(
                bus.emit(Method.CHAT_TOKEN, {"token": accumulated, "turn_id": turn_id})
            )

        self._stream_task = asyncio.create_task(
            self._run_stream(
                text,
                token_callback,
                turn_id,
                provider,
                visual_request,
                session_id,
                str(params.get("source") or ""),
                chat_epoch,
            )
        )
        return {"status": "ok", "turn_id": turn_id}

    async def _interrupt_superseded_turn(self) -> None:
        """Close the one active main turn through the canonical interrupt flow."""

        try:
            from server.interrupt_flow import get_interrupt_flow

            flow = get_interrupt_flow()
            if flow.configured:
                await flow.interrupt(
                    source="new_chat_turn",
                    annotate_history=True,
                )
                return
        except Exception:
            logger.exception("compound interruption before new chat turn failed")
        # Headless/unit configurations may not have a TTS handler.  Reuse the
        # same Chat abort implementation rather than duplicating cancellation
        # or epoch rules here.
        await self._handle_abort({})

    async def _interrupt_background_presentation(self) -> None:
        """Quiesce non-chat speech before issuing the new chat epoch."""

        callback = self._presentation_interrupt
        if callback is None:
            return
        try:
            result = callback()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.exception("background presentation interrupt before chat failed")

    async def _interrupt_background_interaction(self) -> None:
        """Give a confirmed user turn precedence over private background acts."""

        callback = self._background_interaction_interrupt
        if callback is None:
            return
        try:
            result = callback()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.exception("background interaction interrupt before chat failed")

    async def _run_stream(
        self,
        text: str,
        callback,
        turn_id: str,
        provider: Any = None,
        visual_request: Any = None,
        session_id: str = "",
        source: str = "",
        chat_epoch: int = 0,
    ) -> None:
        try:
            branch_result = await self._try_interaction_branch_route(
                text=text,
                turn_id=turn_id,
                session_id=session_id,
            )
            if branch_result is not None:
                if chat_epoch != self._chat_epoch or turn_id != self._active_turn_id:
                    logger.info(
                        "drop stale interaction-branch completion turn_id=%s",
                        turn_id,
                    )
                    return
                full = str(branch_result.get("display_text") or "").strip()
                if full:
                    callback(full)
                self._active_accumulated_text = full
                self._last_assistant_turn_id = turn_id
                self._last_assistant_text = full
                if not await self._turn_allows_visible_emit(turn_id):
                    logger.info("drop pending-discarded chat completion turn_id=%s", turn_id)
                    return
                if session_id and branch_result.get("save_history", True) is not False:
                    self._save_direct_turn(
                        session_id=session_id,
                        user_text=text,
                        assistant_text=full,
                        turn_id=turn_id,
                        branch_id=str(branch_result.get("branch_id") or ""),
                    )
                await bus.emit(
                    Method.CHAT_COMPLETE,
                    {
                        "turn_id": turn_id,
                        "full_text": full,
                        # Direct host/provider branches must remain observable to
                        # clients and acceptance probes.  The normal LLM path has
                        # no route_kind, so consumers can distinguish a
                        # deterministic ledger read from generated conversation
                        # without parsing the answer text.
                        "source": str(branch_result.get("source") or ""),
                        "route_kind": str(branch_result.get("route_kind") or ""),
                        "provider": str(branch_result.get("provider") or ""),
                        "status_fact_kind": str(
                            branch_result.get("status_fact_kind") or ""
                        ),
                        "status_fact_source": str(
                            branch_result.get("status_fact_source") or ""
                        ),
                        "project_id": str(branch_result.get("project_id") or ""),
                        "work_item_id": str(
                            branch_result.get("work_item_id") or ""
                        ),
                        "app_session_id": str(
                            branch_result.get("app_session_id") or ""
                        ),
                        "candidate_id": str(
                            branch_result.get("candidate_id") or ""
                        ),
                        "proposal_id": str(
                            branch_result.get("proposal_id") or ""
                        ),
                        "action_id": str(branch_result.get("action_id") or ""),
                    },
                )
                self._notify_coordinator_finished(turn_id, ok=True)
                voice_receipt: dict[str, Any] = {}
                if full and bool(branch_result.get("speak", True)):
                    voice_receipt = await self._speak_direct_branch_reply(
                        full,
                        branch_result,
                        turn_id=turn_id,
                    )
                await self._notify_direct_branch_delivery(
                    branch_result,
                    visible=True,
                    voice_receipt=voice_receipt,
                )
                if self._on_turn_finished is not None and source == "wake":
                    status = "complete" if full else "empty"
                    result = self._on_turn_finished(
                        {"status": status, "turn_id": turn_id, "source": source}
                    )
                    if hasattr(result, "__await__"):
                        await result
                if turn_id == self._active_turn_id:
                    self._active_turn_id = ""
                    self._active_accumulated_text = ""
                return

            visual_context = await self._prepare_visual_context(text=text, visual_request=visual_request)
            full = await self._stream_llm_query(
                text,
                gui_callback=callback,
                provider=provider,
                visual_context=visual_context,
                turn_id=turn_id,
            )
            if chat_epoch != self._chat_epoch or turn_id != self._active_turn_id:
                logger.info("drop stale chat completion turn_id=%s", turn_id)
                return
            self._active_accumulated_text = str(full or "")
            self._last_assistant_turn_id = turn_id
            self._last_assistant_text = str(full or "")
            if session_id:
                try:
                    os.environ.setdefault("AMADEUS_HEADLESS", "1")
                    from core import session_manager as sm
                    sm.save_session(session_id, enable_conversation=True)
                    if sm.get_session_title(session_id) == session_id:
                        title = text.strip().replace("\n", " ")[:30]
                        if title:
                            sm.set_session_title(session_id, title)
                except Exception:
                    logger.exception("failed to save session %s", session_id)
            if not await self._turn_allows_visible_emit(turn_id):
                logger.info("drop pending-discarded chat completion turn_id=%s", turn_id)
                return
            await bus.emit(Method.CHAT_COMPLETE, {"turn_id": turn_id, "full_text": full})
            self._notify_coordinator_finished(turn_id, ok=True)
            if self._on_turn_finished is not None and source == "wake":
                status = "complete" if str(full or "").strip() else "empty"
                result = self._on_turn_finished(
                    {"status": status, "turn_id": turn_id, "source": source}
                )
                if hasattr(result, "__await__"):
                    await result
            if turn_id == self._active_turn_id:
                self._active_turn_id = ""
                self._active_accumulated_text = ""
        except asyncio.CancelledError:
            logger.info("chat stream cancelled turn_id=%s", turn_id)
            raise
        except Exception as e:
            if chat_epoch != self._chat_epoch or turn_id != self._active_turn_id:
                logger.info("drop stale chat error turn_id=%s", turn_id)
                return
            logger.exception("chat stream error")
            self._notify_coordinator_finished(turn_id, ok=False)
            await bus.emit(Method.CHAT_ERROR, {"turn_id": turn_id, "error": str(e)})
            if self._on_turn_finished is not None and source == "wake":
                result = self._on_turn_finished(
                    {"status": "error", "turn_id": turn_id, "source": source, "error": str(e)}
                )
                if hasattr(result, "__await__"):
                    await result

    async def _try_interaction_branch_route(
        self,
        *,
        text: str,
        turn_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        router = self._interaction_branch_router
        if router is None:
            return None
        try:
            result = router(text=text, session_id=session_id, turn_id=turn_id)
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, dict) and result.get("handled"):
                return result
        except Exception:
            logger.exception("interaction branch router failed; falling back to main chat")
        return None

    @staticmethod
    async def _prepare_visual_context(*, text: str, visual_request: Any = None) -> dict[str, Any] | None:
        try:
            from server import visual_runtime

            return await visual_runtime.prepare_for_chat_turn(text, visual_request)
        except Exception:
            logger.exception("visual runtime failed; continuing without visual context")
            return None

    @staticmethod
    def _save_direct_turn(
        *,
        session_id: str,
        user_text: str,
        assistant_text: str,
        turn_id: str = "",
        branch_id: str = "",
    ) -> None:
        try:
            os.environ.setdefault("AMADEUS_HEADLESS", "1")
            from core import session_manager as sm
            from core.chat_runtime import get_chat_runtime

            if session_id and sm.get_current_session_id() != session_id:
                sm.set_current_session_id(session_id)
            get_chat_runtime().enable_conversation = True
            sm.conversation_history.add_user(str(user_text or ""))
            entry_count = 1
            if assistant_text:
                sm.conversation_history.add_assistant(
                    str(assistant_text or ""),
                    turn_id=turn_id,
                )
                entry_count = 2
            # 快通道直达的分支操作轮打标（squash-merge 区间成员；
            # 正常对白轮不带 branch_id，坍缩时原样保留）
            if branch_id:
                for entry in sm.conversation_history.dialog[-entry_count:]:
                    if isinstance(entry, dict):
                        entry["branch_id"] = str(branch_id)
            sm.save_session(session_id, enable_conversation=True)
            if sm.get_session_title(session_id) == session_id:
                title = str(user_text or "").strip().replace("\n", " ")[:30]
                if title:
                    sm.set_session_title(session_id, title)
        except Exception:
            logger.exception("failed to save direct interaction branch turn")

    async def _speak_direct_branch_reply(
        self,
        text: str,
        branch_result: dict[str, Any],
        *,
        turn_id: str,
    ) -> dict[str, Any]:
        """Render a direct branch answer on the normal character voice lane.

        Browser continuation and deterministic host status reads already own
        their answer text.  The sink performs voice/presentation only; it does
        not reinterpret provider logs or make a second observer decision.
        """
        voice_sink = self._assistant_voice_sink
        if voice_sink is None:
            return {"status": "unavailable", "reason": "voice_sink_missing"}
        line_id = str(
            branch_result.get("line_id")
            or f"direct-branch-{branch_result.get('branch_id') or turn_id}"
        )
        payload = {
            "display_text": str(text or ""),
            "voice_text_ja": str(branch_result.get("voice_text_ja") or ""),
            "emotion": str(branch_result.get("emotion") or "thinking"),
            "duration_ms": 5600,
            "line_id": line_id,
            "turn_id": turn_id,
            "complete_turn": True,
            "source": str(branch_result.get("source") or "browser_conversation_fork"),
            "action": "assistant_reply",
            "terminal": False,
            "branch_id": str(branch_result.get("branch_id") or ""),
            "provider": str(branch_result.get("provider") or "browser"),
        }
        try:
            result = voice_sink(payload)
            if hasattr(result, "__await__"):
                result = await result
            return dict(result) if isinstance(result, dict) else {"status": "unknown"}
        except Exception:
            logger.exception("failed to speak direct branch reply")
            return {"status": "error", "reason": "voice_sink_failed"}

    @staticmethod
    async def _notify_direct_branch_delivery(
        branch_result: dict[str, Any],
        *,
        visible: bool,
        voice_receipt: dict[str, Any],
    ) -> None:
        observer = branch_result.get("delivery_observer")
        if not callable(observer):
            return
        try:
            result = observer(
                {
                    "visible": bool(visible),
                    "voice": dict(voice_receipt or {}),
                }
            )
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.exception("direct branch delivery observer failed")

    @staticmethod
    def _notify_coordinator_finished(turn_id: str, *, ok: bool) -> None:
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_chat_turn_finished(turn_id=turn_id, ok=ok)
        except Exception:
            logger.debug("turn coordinator notify failed", exc_info=True)

    def _advance_chat_epoch(self) -> int:
        """向 TurnCoordinator 账本申领下一 chat epoch（所有权迁移·切片 B）。

        self._chat_epoch 保留为本地只读缓存；账本不可用时回退本地自增。
        """
        try:
            from core.turn_coordinator import get_turn_coordinator

            return get_turn_coordinator().advance_chat_epoch(
                local_next=self._chat_epoch + 1, source="chat_handler"
            )
        except Exception:
            return self._chat_epoch + 1

    def _open_turn(
        self, *, turn_id: str, session_id: str, source: str, pending: bool = False
    ) -> dict[str, Any]:
        """向账本申领轮次（所有权迁移·切片 C/D1）；账本不可用时回退本地自增。"""
        try:
            from core.turn_coordinator import get_turn_coordinator

            return get_turn_coordinator().open_turn(
                turn_id=turn_id,
                local_next_epoch=self._chat_epoch + 1,
                session_id=session_id,
                source=source,
                pending=pending,
            )
        except Exception:
            logger.debug("open_turn via ledger failed; local fallback", exc_info=True)
            return {"turn_id": turn_id, "chat_epoch": self._chat_epoch + 1}

    @staticmethod
    async def _turn_allows_visible_emit(turn_id: str) -> bool:
        if not turn_id:
            return True
        try:
            from core.turn_coordinator import get_turn_coordinator

            coordinator = get_turn_coordinator()
            gate = coordinator.turn_gate(turn_id)
            if gate == "wait":
                gate = await asyncio.to_thread(
                    coordinator.wait_turn_decided,
                    turn_id,
                    PENDING_TURN_GATE_TIMEOUT_S,
                )
            return gate == "proceed"
        except Exception:
            return True

    async def confirm_pending_turn(self, turn_id: str, *, reason: str = "") -> bool:
        """确认投机轮：TTS 门控放行该轮全部条目（pending-turn·切片 D1）。"""
        try:
            from core.turn_coordinator import get_turn_coordinator

            return get_turn_coordinator().confirm_turn(turn_id, reason=reason or "caller_confirm")
        except Exception:
            logger.exception("confirm_pending_turn failed turn=%s", turn_id)
            return False

    async def discard_pending_turn(self, turn_id: str, *, reason: str = "") -> bool:
        """作废投机轮（静默，无打断标注、无历史写入）。

        账本决议使该轮 TTS 条目在出队点被丢弃；若该轮仍是活跃流，
        推进 chat epoch（现有 staleness 检查会丢弃迟到回调）并取消流任务。
        """
        try:
            from core.turn_coordinator import get_turn_coordinator

            ok = get_turn_coordinator().discard_turn(turn_id, reason=reason or "caller_discard")
        except Exception:
            logger.exception("discard_pending_turn failed turn=%s", turn_id)
            ok = False
        if turn_id and turn_id == self._active_turn_id:
            self._chat_epoch = self._advance_chat_epoch()
            self._active_turn_id = ""
            self._active_accumulated_text = ""
            if self._stream_task and not self._stream_task.done():
                self._stream_task.cancel()
        return ok

    async def _handle_abort(self, params: dict[str, Any]) -> dict[str, Any]:
        interrupted_turn_id = self._active_turn_id or self._last_assistant_turn_id
        interrupted_text = self._active_accumulated_text or self._last_assistant_text
        self._chat_epoch = self._advance_chat_epoch()
        self._active_turn_id = ""
        self._active_accumulated_text = ""
        if interrupted_turn_id == self._last_assistant_turn_id:
            self._last_assistant_turn_id = ""
            self._last_assistant_text = ""
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_chat_aborted(turn_id=str(interrupted_turn_id or ""))
        except Exception:
            logger.debug("turn coordinator notify failed", exc_info=True)
        return {
            "status": "aborted",
            "turn_id": interrupted_turn_id,
            "accumulated_text": interrupted_text,
        }
