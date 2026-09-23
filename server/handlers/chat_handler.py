"""Adapter for LLM chat pipeline – wraps stream_llm_query from main.py."""

from __future__ import annotations

import asyncio
from collections import ChainMap
from copy import deepcopy
from dataclasses import replace
import logging
import os
import uuid
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.session_manager import ConversationHistory
    from server.control_ledger import ControlLedgerStore
    from server.turn_admission import TurnAdmissionRecord

from config.settings import PENDING_TURN_GATE_TIMEOUT_S
from core.turn_coordinator import TurnAuthorityError
from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class ChatHandler(RequestHandler):
    methods = [Method.CHAT_SEND, Method.CHAT_ABORT, Method.CHAT_PERMISSION_RESOLVE,
        Method.CHAT_TRANSLATE]

    def __init__(self) -> None:
        self._stream_task: asyncio.Task | None = None
        self._stream_llm_query = None          # injected by configure()
        self._pending_sentence_items = None
        self._on_turn_finished = None
        self._interaction_branch_router = None
        self._assistant_voice_sink = None
        self._presentation_interrupt = None
        self._background_interaction_interrupt = None
        self._abort_sink = None
        self._permission_sink = None
        self._chat_epoch = 0
        self._active_turn_id = ""
        self._active_accumulated_text = ""
        self._last_assistant_turn_id = ""
        self._last_assistant_text = ""
        self._pending_user_events: dict[str, dict[str, str]] = {}
        self._control_ledger: ControlLedgerStore | None = None
        self._control_fence_scope = ""
        self._control_authority_mode = ""
        self._control_root_id = ""
        self._control_turn_runner = None
        self._control_admission_preparer = None
        self._control_allows_pending = False
        self._control_ingress_lock = asyncio.Lock()
        self._ingress_tasks: dict[asyncio.Task, str] = {}
        self._stream_tasks: set[asyncio.Task] = set()
        self._closed = False
        self._close_task: asyncio.Task | None = None

    def configure(
        self,
        stream_llm_query,
        pending_sentence_items,
        on_turn_finished=None,
        interaction_branch_router=None,
        assistant_voice_sink=None,
        presentation_interrupt=None,
        background_interaction_interrupt=None,
        abort_sink=None,
        permission_sink=None,
    ) -> None:
        self._stream_llm_query = stream_llm_query
        self._pending_sentence_items = pending_sentence_items
        self._on_turn_finished = on_turn_finished
        self._interaction_branch_router = interaction_branch_router
        self._assistant_voice_sink = assistant_voice_sink
        self._presentation_interrupt = presentation_interrupt
        self._background_interaction_interrupt = background_interaction_interrupt
        self._abort_sink = abort_sink
        self._permission_sink = permission_sink

    def configure_control_ingress(
        self,
        ledger: ControlLedgerStore,
        *,
        fence_scope: str,
        authority_mode: str,
        turn_runner=None,
        admission_preparer=None,
        allows_pending: bool = False,
    ) -> None:
        """Install one explicit Host source-admission assembly.

        Install once while quiescent. The mode is a Host cohort, never a
        request/model field. New mode needs its own whole-turn runner. Session
        assembly separately installs invalidate_session_context as its guard.
        The default app bootstrap leaves this unset; the cooperative opt-in
        installs the existing legacy cohort with a pre-admission context hook.
        """
        from core.turn_coordinator import get_turn_coordinator

        if self._closed or self._control_ledger is not None or self.is_busy():
            raise TurnAuthorityError("control ingress requires a new quiescent Handler")
        if not isinstance(fence_scope, str) or not fence_scope.strip():
            raise ValueError("an explicit foreground fence scope is required")
        if authority_mode not in {"legacy", "turn_decision"}:
            raise ValueError("an explicit Host authority mode is required")
        coordinator = get_turn_coordinator()
        snapshot = coordinator.snapshot()
        if snapshot["active_turn_id"]:
            raise TurnAuthorityError("the foreground owner is not quiescent")
        fence = ledger.get_epoch_fence(fence_scope)
        epoch = max(self._chat_epoch, snapshot["epochs"]["chat"], fence["chat_epoch"] if fence else 0)
        coordinator.synchronize_chat_epoch(epoch, source="control_ingress_restore")
        self._chat_epoch = epoch
        self._control_ledger = ledger
        self._control_fence_scope = fence_scope
        self._control_authority_mode = authority_mode
        self._control_root_id = fence["root_id"] if fence else ""
        self._control_turn_runner = turn_runner
        self._control_admission_preparer = admission_preparer
        self._control_allows_pending = bool(allows_pending and authority_mode == "turn_decision"
            and turn_runner is not None)

    def _control_replay(self, admission: TurnAdmissionRecord) -> dict[str, Any] | None:
        stored = self._control_ledger.find_admission(
            admission.dialogue_source_scope, admission.utterance_id,
        )
        if stored is not None and (
            stored["root_id"], stored["fence_scope"], stored["transcript_hash"],
        ) != (admission.root_id, self._control_fence_scope, admission.transcript_hash):
            raise TurnAuthorityError("transport replay changed its source identity")
        return stored

    @staticmethod
    def _control_replay_result(admission: TurnAdmissionRecord, stored) -> dict[str, Any]:
        # Current admission eligibility is not producer activity or completion.
        return {
            "status": "replayed", "turn_id": admission.turn_id,
            "root_id": stored["root_id"], "chat_epoch": stored["chat_epoch"],
            "authority_mode": stored["authority_mode"],
            "admission_lifecycle": stored["lifecycle"], "plan_id": stored["plan_id"],
        }

    def _open_control_turn(self, admission: TurnAdmissionRecord):
        from core.turn_coordinator import get_turn_coordinator

        coordinator = get_turn_coordinator()
        minimum = max(self._chat_epoch, coordinator.snapshot()["epochs"]["chat"]) + 1
        result = self._control_ledger.open_admission(
            root_id=admission.root_id, source_scope=admission.dialogue_source_scope,
            fence_scope=self._control_fence_scope, utterance_id=admission.utterance_id,
            authority_mode=admission.authority_mode, transcript_hash=admission.transcript_hash,
            minimum_epoch=minimum,
        )
        stored = result["admission"]
        if result["replayed"]:
            return None, self._control_replay_result(admission, stored)
        granted = replace(admission, chat_epoch=stored["chat_epoch"])
        self._control_root_id = granted.root_id
        try:
            self._open_turn(
                turn_id=granted.turn_id, session_id=granted.session_id,
                source=granted.input_source, pending=granted.pending, granted_epoch=granted.chat_epoch,
            )
        except BaseException:
            self._retire_control_turn(granted)
            raise
        return granted, None

    def _retire_control_turn(self, admission: TurnAdmissionRecord | None) -> None:
        if self._control_ledger is None or admission is None:
            return
        try:
            # Never look up whichever root became current later.
            self._control_ledger.discard(admission.root_id)
        except Exception:
            logger.exception("failed to retire exact control root after local turn failure")

    def invalidate_session_context(self, previous: str | None, next_session: str | None) -> None:
        """Synchronous callback for the existing Session pre-activation boundary."""
        if self._control_ledger is None:
            raise TurnAuthorityError("Session control ingress is not configured")
        try:
            self._chat_epoch = self._advance_chat_epoch()
        finally:
            self._active_turn_id = ""
            self._active_accumulated_text = ""
            self._last_assistant_turn_id = ""
            self._last_assistant_text = ""
            self._cancel_task_once(self._stream_task)

    def is_busy(self) -> bool:
        return bool(
            self._active_turn_id
            or any(not task.done() for task in self._ingress_tasks)
            or (self._stream_task is not None and not self._stream_task.done())
        )

    @property
    def foreground_turn_id(self) -> str:
        return self._active_turn_id or self._last_assistant_turn_id

    def _cancel_ingress(self, turn_id: str = "") -> bool:
        cancelled = False
        for task, candidate in self._ingress_tasks.items():
            if not task.done() and (not turn_id or candidate == turn_id):
                cancelled = self._cancel_task_once(task) or cancelled
        return cancelled

    @staticmethod
    def _cancel_task_once(task: asyncio.Task | None) -> bool:
        if task is None or task.done():
            return False
        # A second cancellation could interrupt the task's resource cleanup.
        if not task.cancelling():
            task.cancel()
        return True

    async def close(self) -> None:
        """Drain Chat producers before their shared Provider/Work dependencies.

        Closing the transport caller must not cancel this owned cleanup. This is
        foreground retirement, not global Control Ledger rollback or store close.
        """
        if self._close_task is None:
            self._closed = True
            self._cancel_ingress()
            self._close_task = asyncio.create_task(self._close_owned())
        await asyncio.shield(self._close_task)

    async def _close_owned(self) -> None:
        tasks = set(self._ingress_tasks) | self._stream_tasks
        if self._stream_task is not None:
            tasks.add(self._stream_task)
        try:
            await self._handle_abort({})
        finally:
            for task in tasks:
                self._cancel_task_once(task)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.CHAT_SEND:
            return await self._handle_send(params)
        if method == Method.CHAT_ABORT:
            return await self._handle_abort(params)
        if method == Method.CHAT_PERMISSION_RESOLVE:
            if self._permission_sink is None:
                raise TurnAuthorityError("cooperative permission interaction is unavailable")
            result = self._permission_sink(dict(params or {}))
            if hasattr(result, "__await__"):
                result = await result
            return dict(result) if isinstance(result, dict) else {"ok":True}
        if method == Method.CHAT_TRANSLATE:
            return await self._handle_translate(params)
        return None

    @staticmethod
    async def _handle_translate(params: dict[str, Any]) -> dict[str, Any]:
        """Return a derived GUI subtitle without touching Chat history."""

        from server.chat_translation_runtime import translate_completed_message

        result = await translate_completed_message(params.get("text", ""))
        result["turn_id"] = str(params.get("turn_id") or "")
        return result

    async def send_text(
        self,
        text: str,
        *,
        provider: Any = None,
        visual: Any = None,
        session_id: str = "",
        turn_id: str = "",
        source: str = "",
        pending: bool = False,
    ) -> dict[str, Any]:
        return await self._handle_send(
            {
                "text": text,
                "provider": provider,
                "visual": visual,
                "session_id": session_id,
                "turn_id": turn_id or uuid.uuid4().hex,
                "source": source,
                "pending": pending,
            }
        )

    async def _handle_send(self, params: dict[str, Any]) -> dict[str, Any]:
        from core import session_manager as sm

        if self._closed:
            raise TurnAuthorityError("Chat ingress is closed")
        # Capture only consumed transport fields. Source evidence has its existing
        # bounded projection; cloning the entire request would bypass that bound.
        session_id = str(params.get("session_id") or sm.get_current_session_id() or "")
        text = str(params.get("text") or "")
        turn_id = str(params.get("turn_id") or "")
        source = str(params.get("source") or "")
        pending = bool(params.get("pending", False))
        prepared = self._capture_turn_admission(
            utterance_id=str(params.get("utterance_id") or turn_id),
            turn_id=turn_id, session_id=session_id, text=text, source=source,
            chat_epoch=None, pending=pending, source_evidence=params.get("source_evidence"),
            utterance_identity_source="explicit_utterance_id" if params.get("utterance_id") else "turn_id_fallback",
            authority_mode=self._control_authority_mode if self._control_ledger is not None else "source_witness_v1",
        )
        captured_params = {
            "text": text, "turn_id": turn_id, "source": source, "session_id": session_id,
            "pending": pending, "provider": params.get("provider"),
            "visual": deepcopy(params.get("visual")),
        }
        selection_revision = sm.get_session_selection_revision() if self._control_ledger is not None else None
        # Never cancel the transport's enclosing Task (e.g. the WS read loop).
        task = asyncio.create_task(self._run_ingress_setup(captured_params, prepared, selection_revision))
        self._ingress_tasks[task] = turn_id
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            return {"status": "cancelled_before_admission", "turn_id": turn_id}
        finally:
            self._ingress_tasks.pop(task, None)

    async def _run_ingress_setup(
        self, params: dict[str, Any], prepared: TurnAdmissionRecord | None,
        selection_revision: int | None,
    ) -> dict[str, Any]:
        if self._control_ledger is None:
            return await self._handle_send_owned(params, prepared=prepared)
        try:
            # Serialize setup only; generation runs in its existing Task.
            async with self._control_ingress_lock:
                return await self._handle_send_owned(params, prepared=prepared, selection_revision=selection_revision)
        except TurnAuthorityError:
            raise
        except Exception as exc:
            raise TurnAuthorityError(f"managed Chat ingress failed: {exc}") from exc

    async def _handle_send_owned(
        self, params: dict[str, Any], *, prepared: TurnAdmissionRecord | None,
        selection_revision: int | None = None,
    ) -> dict[str, Any]:
        from core import session_manager as sm

        text = params.get("text", "")
        turn_id = params.get("turn_id", "")
        provider = params.get("provider", None)
        visual_request = params.get("visual", None)
        pending = bool(params.get("pending", False))
        session_id = str(params.get("session_id") or "")
        if self._control_ledger is not None:
            if (prepared is None or not session_id or prepared.session_id != session_id
                    or (pending and (not self._control_allows_pending
                        or session_id != sm.get_current_session_id()))):
                raise TurnAuthorityError("managed ingress requires supported, Session-bound source identity")
            replay = self._control_replay(prepared)
            if replay is not None:
                return self._control_replay_result(prepared, replay)
            sm.require_session_selection(selection_revision)
            if not os.path.exists(sm._session_path(session_id)):
                raise TurnAuthorityError("managed ingress requires an already prepared Session")
            if self._control_authority_mode == "turn_decision" and self._control_turn_runner is None:
                raise TurnAuthorityError("TurnDecision whole-turn runner is not configured")
        if self._stream_llm_query is None and not (
            prepared is not None and prepared.authority_mode == "turn_decision" and self._control_turn_runner is not None
        ):
            raise RuntimeError("chat handler not configured")
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
        sm.require_session_selection(selection_revision)
        if not pending:
            await self._interrupt_background_interaction()
        # Close the previous foreground turn before selecting this input's
        # Session. Relabelling the global history with a new id is not a load.
        os.environ.setdefault("AMADEUS_HEADLESS", "1")
        from core import session_manager as sm
        from core.chat_runtime import get_chat_runtime

        sm.require_session_selection(selection_revision)
        if self._control_ledger is not None and not os.path.exists(sm._session_path(session_id)):
            raise TurnAuthorityError("the input's prepared Session no longer exists")

        if session_id:
            if sm.get_current_session_id() != session_id:
                previous_session = sm.get_current_session_id()
                if previous_session:
                    if not sm.save_session(previous_session, enable_conversation=True):
                        raise RuntimeError("could not save the active Session before switching")
                if os.path.exists(sm._session_path(session_id)):
                    selection = {} if selection_revision is None else {"expected_selection_revision": selection_revision}
                    if not sm.load_session(session_id, **selection)[0]:
                        raise RuntimeError("could not load the requested Session context")
                else:
                    sm.create_session(session_id)
            get_chat_runtime().enable_conversation = True
        history_snapshot = sm.conversation_history.snapshot()
        loop = asyncio.get_running_loop()
        if self._control_admission_preparer is not None:
            prepared_context = self._control_admission_preparer(prepared)
            if hasattr(prepared_context, "__await__"):
                await prepared_context
        # 向账本申领轮次：epoch 发放 + 身份登记 + 重叠校验一次原子完成
        # 申领失败不能用本地计数伪造 grant；此时不启动模型或 direct child。
        # pending=True 开投机轮：LLM 照常流式，TTS 条目在出队点被扣住待决议
        if self._control_ledger is not None:
            turn_admission, replay = self._open_control_turn(prepared)
            if replay is not None:
                return replay
            self._chat_epoch = int(turn_admission.chat_epoch)
        else:
            grant = self._open_turn(
                turn_id=turn_id, session_id=session_id,
                source=str(params.get("source") or ""), pending=pending,
            )
            self._chat_epoch = int(grant["chat_epoch"])
            turn_admission = replace(prepared, chat_epoch=self._chat_epoch) if prepared is not None else None
        chat_epoch = self._chat_epoch
        self._active_turn_id = turn_id
        self._active_accumulated_text = ""
        user_event = {
            "turn_id": turn_id,
            "text": str(text or ""),
            "session_id": session_id,
            "source": str(params.get("source") or ""),
        }
        # Every confirmed turn has one authoritative user-message event.  The
        # desktop chat consumes it alongside turns submitted by other input
        # surfaces (for example, the wallpaper keyboard), instead of each
        # surface maintaining its own partial chat transcript.  A pending turn
        # must wait for its ledger decision so discarded speculation never
        # appears in the transcript.
        if not pending:
            await bus.emit(Method.CHAT_USER, user_event)
        else:
            self._pending_user_events[turn_id] = user_event
        interaction_branch_routing_lease = (
            self._capture_interaction_branch_routing_lease(session_id)
        )
        self._observe_turn_admission(turn_admission)

        def token_callback(accumulated: str) -> None:
            if chat_epoch != self._chat_epoch or turn_id != self._active_turn_id:
                return
            if pending:
                return
            self._active_accumulated_text = str(accumulated or "")
            loop.create_task(
                bus.emit(Method.CHAT_TOKEN, {"token": accumulated, "turn_id": turn_id,
                    "session_id": session_id})
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
                interaction_branch_routing_lease,
                turn_admission,
                history_snapshot,
            )
        )
        self._stream_tasks.add(self._stream_task)

        def observe_stream_done(task: asyncio.Task) -> None:
            self._stream_tasks.discard(task)
            # Also covers cancellation before _run_stream's first instruction.
            # Runtime's more specific failed/superseded evidence wins if present.
            if task.cancelled():
                self._retire_control_turn(turn_admission)
            try:
                from server.turn_decision_shadow import get_enabled_turn_decision_shadow_observer

                observer = get_enabled_turn_decision_shadow_observer()
                if observer is not None:
                    status = (
                        "cancelled" if task.cancelled()
                        else "failed" if task.exception() is not None else "completed"
                    )
                    observer.mark_lifecycle(
                        turn_id, status, reason="chat_stream_task_done", only_if_open=True,
                    )
            except Exception:
                logger.debug("chat stream terminal observation failed", exc_info=True)

        self._stream_task.add_done_callback(observe_stream_done)
        return {"status": "ok", "turn_id": turn_id}

    async def _interrupt_superseded_turn(self) -> None:
        """Close the one active main turn through the canonical interrupt flow."""

        from server.interrupt_flow import get_interrupt_flow

        flow = get_interrupt_flow()
        old_turn_id = self.foreground_turn_id
        if flow.configured:
            await flow.interrupt(source="new_chat_turn", annotate_history=True, turn_id=old_turn_id)
            return
        # Headless/unit configurations may not have a TTS handler.  Reuse the
        # same Chat abort implementation rather than duplicating cancellation
        # or epoch rules here.
        await self._handle_abort({"turn_id": old_turn_id, "stop_execution": False})

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
        interaction_branch_routing_lease: dict[str, Any] | None = None,
        turn_admission: TurnAdmissionRecord | None = None,
        history_snapshot: ConversationHistory | None = None,
    ) -> None:
        try:
            new_mode = bool(
                turn_admission is not None
                and turn_admission.authority_mode == "turn_decision"
            )
            branch_result = None
            if not new_mode:
                branch_result = await self._try_interaction_branch_route(
                    text=text, turn_id=turn_id, session_id=session_id,
                    routing_scope=interaction_branch_routing_lease,
                    turn_admission=turn_admission,
                )
            if branch_result is not None:
                if chat_epoch != self._chat_epoch or turn_id != self._active_turn_id:
                    logger.info(
                        "drop stale interaction-branch completion turn_id=%s",
                        turn_id,
                    )
                    return
                self._observe_direct_branch(turn_id, branch_result)
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
                        "session_id": session_id,
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
            runner = self._control_turn_runner if new_mode else self._stream_llm_query
            if runner is None:
                raise TurnAuthorityError("the admitted turn has no execution owner")
            full = await runner(
                text,
                gui_callback=callback,
                provider=provider,
                visual_context=visual_context,
                turn_id=turn_id,
                interaction_branch_routing_lease=(
                    dict(interaction_branch_routing_lease or {})
                ),
                turn_admission=turn_admission,
                history_snapshot=history_snapshot,
            )
            if chat_epoch != self._chat_epoch or turn_id != self._active_turn_id:
                logger.info("drop stale chat completion turn_id=%s", turn_id)
                return
            from server.handlers.session_handler import _display_text

            visible_full = str(_display_text(full) or "")
            self._active_accumulated_text = visible_full
            self._last_assistant_turn_id = turn_id
            self._last_assistant_text = visible_full
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
            await bus.emit(
                Method.CHAT_COMPLETE,
                {"turn_id": turn_id, "session_id": session_id, "full_text": visible_full},
            )
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
            self._retire_control_turn(turn_admission)
            if chat_epoch != self._chat_epoch or turn_id != self._active_turn_id:
                logger.info("drop stale chat error turn_id=%s", turn_id)
                return
            if turn_admission is not None and turn_admission.pending:
                from core.turn_coordinator import get_turn_coordinator

                if get_turn_coordinator().turn_gate(turn_id) != "proceed":
                    await self.discard_pending_turn(turn_id, reason="pending_interpretation_failed")
                    return
            logger.exception("chat stream error")
            self._notify_coordinator_finished(turn_id, ok=False)
            await bus.emit(Method.CHAT_ERROR, {"turn_id": turn_id, "session_id": session_id,
                "error": str(e)})
            if self._on_turn_finished is not None and source == "wake":
                result = self._on_turn_finished(
                    {"status": "error", "turn_id": turn_id, "source": source, "error": str(e)}
                )
                if hasattr(result, "__await__"):
                    await result
            if turn_id == self._active_turn_id:
                self._active_turn_id = ""
                self._active_accumulated_text = ""

    async def _try_interaction_branch_route(
        self,
        *,
        text: str,
        turn_id: str,
        session_id: str,
        routing_scope: dict[str, Any] | None = None,
        turn_admission: TurnAdmissionRecord | None = None,
    ) -> dict[str, Any] | None:
        router = self._interaction_branch_router
        if router is None:
            return None
        try:
            result = router(
                text=text,
                session_id=session_id,
                turn_id=turn_id,
                routing_scope=(
                    dict(routing_scope) if routing_scope is not None else None
                ),
                turn_admission=turn_admission,
            )
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, dict):
                transition = result.get("routing_scope_transition")
                if isinstance(transition, dict) and routing_scope is not None:
                    routing_scope.clear()
                    routing_scope.update(dict(transition))
                if result.get("handled"):
                    return result
        except Exception as exc:
            logger.exception("interaction branch router failed")
            scope_state = (
                str(routing_scope.get("state") or "").strip().lower()
                if isinstance(routing_scope, dict)
                else ""
            )
            if scope_state in {
                "bound",
                "absent",
                "reserved",
                "quarantined",
                "invalid",
            }:
                # The direct route may have crossed an execution boundary before
                # raising. Never invoke a second planner for the same utterance
                # when effect state is unknown.
                return {
                    "handled": True,
                    "route_kind": "interaction_route_failed_closed",
                    "branch_id": str(
                        routing_scope.get("branch_id") or ""
                    ),
                    "provider": "browser",
                    "display_text": (
                        "The interaction route failed before I could confirm its "
                        "execution state, so I did not submit the same request again."
                    ),
                    "voice_text_ja": (
                        "操作の実行状態を確認できないまま経路で問題が起きたため、"
                        "同じ依頼は重ねて送っていないわ。"
                    ),
                    "speak": True,
                    "execution_uncertain": True,
                    "continuation_disposition": "failed",
                    "continuation_reason": (
                        f"interaction_route_error:{type(exc).__name__}"
                    ),
                }
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
                # A direct Browser reply can complete after the user switches
                # conversations. Never replace the globally loaded transcript
                # merely to persist that stale completion.
                logger.info(
                    "skip direct branch history for inactive session=%s current=%s",
                    session_id,
                    sm.get_current_session_id() or "",
                )
                return
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
        try:
            from server.turn_decision_shadow import (
                get_enabled_turn_decision_shadow_observer,
            )

            shadow = get_enabled_turn_decision_shadow_observer()
            if shadow is not None:
                shadow.mark_lifecycle(
                    turn_id,
                    "completed" if ok else "failed",
                    only_if_open=True,
                )
        except Exception:
            logger.debug("turn decision lifecycle observation failed", exc_info=True)

    @staticmethod
    def _capture_interaction_branch_routing_lease(
        session_id: str,
    ) -> dict[str, Any]:
        """Freeze Browser branch identity at Host turn admission."""

        try:
            from server.interaction_branch import (
                capture_interaction_branch_routing_scope,
            )

            return capture_interaction_branch_routing_scope(session_id)
        except Exception:
            logger.debug("interaction branch lease capture failed", exc_info=True)
            return {
                "state": "invalid",
                "parent_session_id": str(session_id or "").strip(),
                "reason": "routing_scope_capture_failed",
            }

    @staticmethod
    def _capture_turn_admission(
        *,
        utterance_id: str,
        turn_id: str,
        session_id: str,
        text: str,
        source: str,
        chat_epoch: int | None,
        pending: bool,
        source_evidence: Any,
        utterance_identity_source: str,
        authority_mode: str = "source_witness_v1",
    ) -> TurnAdmissionRecord | None:
        from server.turn_admission import capture_turn_admission

        return capture_turn_admission(
            utterance_id=utterance_id, turn_id=turn_id, session_id=session_id,
            transcript=text, input_source=source, chat_epoch=chat_epoch,
            pending=pending, authority_mode=authority_mode,
            source_evidence=ChainMap(
                {"utterance_identity_source": utterance_identity_source},
                source_evidence if isinstance(source_evidence, dict) else {},
            ),
        )

    @staticmethod
    def _observe_turn_admission(captured: TurnAdmissionRecord | None) -> None:
        try:
            from server.turn_decision_shadow import (
                get_enabled_turn_decision_shadow_observer,
            )

            shadow = get_enabled_turn_decision_shadow_observer()
            if shadow is not None:
                shadow.observe_admission(captured)
        except Exception:
            logger.debug("turn decision admission observation failed", exc_info=True)

    @staticmethod
    def _observe_direct_branch(turn_id: str, result: dict[str, Any]) -> None:
        try:
            from server.turn_decision_shadow import (
                get_enabled_turn_decision_shadow_observer,
            )

            shadow = get_enabled_turn_decision_shadow_observer()
            if shadow is not None:
                shadow.observe_direct_branch(turn_id, result)
        except Exception:
            logger.debug("direct branch decision observation failed", exc_info=True)

    def _advance_chat_epoch(self) -> int:
        """向 TurnCoordinator 账本申领下一 chat epoch（所有权迁移·切片 B）。

        self._chat_epoch 保留为本地只读缓存；账本不可用时拒绝发放。
        """
        try:
            from core.turn_coordinator import get_turn_coordinator

            if self._control_ledger is not None:
                fence = self._control_ledger.get_epoch_fence(self._control_fence_scope)
                if fence is None:
                    if self._control_root_id:
                        raise TurnAuthorityError("the durable foreground fence disappeared")
                    return self._chat_epoch
                if fence["root_id"] != self._control_root_id:
                    raise TurnAuthorityError("the durable foreground owner changed")
                coordinator = get_turn_coordinator()
                minimum = max(self._chat_epoch, coordinator.snapshot()["epochs"]["chat"]) + 1
                advanced = self._control_ledger.advance_epoch(
                    fence_scope=self._control_fence_scope,
                    expected_epoch=fence["chat_epoch"], minimum_epoch=minimum,
                )
                epoch = int(advanced["chat_epoch"])
                coordinator.synchronize_chat_epoch(epoch, source="control_ingress_invalidate")
                return epoch
            return get_turn_coordinator().advance_chat_epoch(
                local_next=self._chat_epoch + 1, source="chat_handler"
            )
        except TurnAuthorityError:
            raise
        except Exception as exc:
            raise TurnAuthorityError("Chat epoch owner unavailable") from exc

    def _open_turn(
        self, *, turn_id: str, session_id: str, source: str, pending: bool = False,
        granted_epoch: int | None = None,
    ) -> dict[str, Any]:
        """Request a turn from its owner; never manufacture a local grant."""
        try:
            from core.turn_coordinator import get_turn_coordinator

            kwargs = dict(
                turn_id=turn_id,
                local_next_epoch=self._chat_epoch + 1,
                session_id=session_id,
                source=source,
                pending=pending,
            )
            if granted_epoch is not None:
                kwargs["granted_epoch"] = granted_epoch
            return get_turn_coordinator().open_turn(**kwargs)
        except TurnAuthorityError:
            raise
        except Exception as exc:
            raise TurnAuthorityError("Chat turn owner unavailable") from exc

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

            confirmed = get_turn_coordinator().confirm_turn(
                turn_id, reason=reason or "caller_confirm"
            )
        except TurnAuthorityError:
            raise
        except Exception:
            logger.exception("confirm_pending_turn failed turn=%s", turn_id)
            return False
        user_event = self._pending_user_events.pop(turn_id, None)
        if confirmed:
            if user_event is not None:
                await bus.emit(Method.CHAT_USER, user_event)
        return confirmed

    async def discard_pending_turn(self, turn_id: str, *, reason: str = "") -> bool:
        """作废投机轮（静默，无打断标注、无历史写入）。

        账本决议使该轮 TTS 条目在出队点被丢弃；若该轮仍是活跃流，
        推进 chat epoch（现有 staleness 检查会丢弃迟到回调）并取消流任务。
        """
        active = bool(turn_id and turn_id == self._active_turn_id)
        try:
            try:
                from core.turn_coordinator import get_turn_coordinator

                ok = get_turn_coordinator().discard_turn(turn_id, reason=reason or "caller_discard")
            except TurnAuthorityError:
                raise
            except Exception:
                logger.exception("discard_pending_turn failed turn=%s", turn_id)
                ok = False
            if active:
                self._chat_epoch = self._advance_chat_epoch()
        finally:
            if active:
                self._active_turn_id = ""
                self._active_accumulated_text = ""
                self._cancel_task_once(self._stream_task)
        try:
            from server.turn_decision_shadow import (
                get_enabled_turn_decision_shadow_observer,
            )

            shadow = get_enabled_turn_decision_shadow_observer()
            if shadow is not None:
                shadow.mark_lifecycle(
                    turn_id,
                    "discarded",
                    reason=reason or "caller_discard",
                )
        except Exception:
            logger.debug("discard lifecycle observation failed", exc_info=True)
        self._pending_user_events.pop(turn_id, None)
        return ok

    async def _handle_abort(self, params: dict[str, Any]) -> dict[str, Any]:
        from core import session_manager as sm

        expected_turn_id = str(params.get("turn_id") or "").strip()
        cancelled_setup = self._cancel_ingress(expected_turn_id)
        current_turn_id = self.foreground_turn_id
        if expected_turn_id and expected_turn_id != current_turn_id:
            return {
                "status": "cancelled_before_admission" if cancelled_setup else "stale",
                "turn_id": expected_turn_id,
                "accumulated_text": "",
            }
        if self._active_turn_id:
            interrupted_turn_id = self._active_turn_id
            interrupted_text = self._active_accumulated_text
        else:
            interrupted_turn_id = self._last_assistant_turn_id
            interrupted_text = self._last_assistant_text
        try:
            self._chat_epoch = self._advance_chat_epoch()
        finally:
            # Local cleanup is still required when the authoritative fence
            # fails. The exception propagates; cleanup is not a success receipt.
            self._active_turn_id = ""
            self._active_accumulated_text = ""
            if interrupted_turn_id == self._last_assistant_turn_id:
                self._last_assistant_turn_id = ""
                self._last_assistant_text = ""
            self._cancel_task_once(self._stream_task)
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_chat_aborted(turn_id=str(interrupted_turn_id or ""))
        except Exception:
            logger.debug("turn coordinator notify failed", exc_info=True)
        execution_stop = None
        if (params.get("stop_execution", True) is not False
                and self._abort_sink is not None and interrupted_turn_id):
            try:
                execution_stop = self._abort_sink(
                    str(interrupted_turn_id), str(sm.get_current_session_id() or "")
                )
                if hasattr(execution_stop, "__await__"):
                    execution_stop = await execution_stop
            except Exception as exc:
                logger.exception("accepted execution stop failed during Chat abort")
                execution_stop = {
                    "state": "unknown",
                    "reason": f"execution_stop_failed:{type(exc).__name__}",
                }
        try:
            from server.turn_decision_shadow import (
                get_enabled_turn_decision_shadow_observer,
            )

            shadow = get_enabled_turn_decision_shadow_observer()
            if shadow is not None:
                shadow.mark_lifecycle(
                    str(interrupted_turn_id or ""),
                    "superseded",
                    reason="chat_abort",
                )
        except Exception:
            logger.debug("abort lifecycle observation failed", exc_info=True)
        result = {
            "status": "aborted",
            "turn_id": interrupted_turn_id,
            "accumulated_text": interrupted_text,
        }
        if execution_stop is not None:
            result["execution_stop"] = execution_stop
        return result
