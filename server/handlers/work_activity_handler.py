"""Map provider work events to wallpaper scene activity and canvas signals."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

from config import settings
from agent_host.provider_identity import PARENT_CONTEXT_DELIVERED_EVENT
from server.character_presentation import coordinator as character_presentation
from server.ai_os_schema import (
    action_ref,
    browser_canvas_payload,
    canvas_payload,
    markdown_canvas_payload,
    presentation_message,
    work_note_payload,
    work_signal,
)
from server.event_bus import bus
from server.protocol import Method
from server.work_context import add_work_note
from server.work_semantic_progress import (
    SemanticProgressFact,
    consume_tool_call,
    remember_tool_call,
    semantic_progress_fact,
)

logger = logging.getLogger(__name__)


class WorkActivityCoordinator:
    """Turn provider execution facts into render-level work semantics.

    This coordinator deliberately stays above individual providers. Codex,
    OpenClaw, or a future provider can emit the same provider events, and
    the UI/runtime gets the same semantic behavior:
    - behavior graph: a `work` SpriteForge intent
    - scene graph: wallpaper `work` activity
    - canvas: compact visual progress/result cards

    It does not speak. Provider events are execution facts, not character
    performance. Spoken narration should be decided by the main chat/narrative
    layer after these facts are compressed into user-facing work notes.
    """

    _WORK_START_EVENTS = {
        "run.created",
        "run.started",
        "tool.call",
        "tool.result",
        "artifact.created",
        "diff.updated",
    }
    _WORK_TERMINAL_EVENTS = {
        "run.finished",
        "run.failed",
        "run.cancelled",
    }
    _RUNNING_STATUSES = {"queued", "running", "active", "working"}
    _TERMINAL_STATUSES = {"done", "error", "failed", "cancelled", "canceled"}
    _BEHAVIOR_THROTTLE_SECONDS = 6.0
    _OBSERVER_RELEASE_FALLBACK_S = 35.0
    _MIN_STREAM_TEXT_CHARS = 24
    _PERMISSION_REQUEST_EVENTS = {"permission.requested", "permission.required"}
    _PERMISSION_RESOLUTION_EVENTS = {
        "permission.resolved",
        "permission.allowed",
        "permission.approved",
        "permission.granted",
        "permission.denied",
        "permission.rejected",
        "permission.expired",
    }
    _MAX_PENDING_PERMISSIONS = 8
    _MAX_PERMISSION_SCOPE = 8
    _MAX_PERMISSION_OPTIONS = 6
    _SECRET_ASSIGNMENT_RE = re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization)\b"
        r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
    )
    _BEARER_SECRET_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")
    _KEY_SECRET_RE = re.compile(r"\b(?:sk|rk|pk|api)[-_][A-Za-z0-9_-]{8,}\b", re.I)

    def __init__(self) -> None:
        self._subscribed = False
        self._active_runs: set[str] = set()
        self._runs: dict[str, dict[str, Any]] = {}
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}
        self._last_behavior_intent_at = 0.0

    def configure(self) -> None:
        if self._subscribed:
            return
        bus.on(Method.PROVIDER_EVENT, self._on_provider_event)
        bus.on(Method.PROVIDER_RESULT, self._on_provider_result)
        self._subscribed = True

    async def _on_provider_event(self, _method: str, params: dict[str, Any]) -> None:
        event_type = str(params.get("type") or "").strip().lower()
        if event_type == PARENT_CONTEXT_DELIVERED_EVENT:
            # Cursor authority is durable control-plane evidence, not visible
            # execution progress, liveness, narration, or Canvas activity.
            return
        run_id = str(params.get("run_id") or "").strip()
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        state = self._run_state(params)
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        if metadata:
            state["metadata"] = self._merge_run_metadata(state.get("metadata"), metadata)
        is_replay = bool(metadata.get("replay"))
        liveness_signal = str(payload.get("liveness") or "").strip().lower()
        if not (
            event_type == "run.status"
            and liveness_signal in {"quiet", "stalled", "cancel_pending"}
        ):
            state["last_provider_activity_at"] = time.monotonic()

        if self._is_steer_replacement_cancellation(state, event_type):
            # A non-native provider is being stopped only so the same durable
            # WorkItem can continue in a replacement attempt.  Rendering the
            # predecessor as Review/cancelled would briefly make the UI and
            # narrator claim that the user's task ended.  The ledger still
            # records the cancellation; WorkActivity only releases this run's
            # presentation ownership and waits for the successor attempt.
            state["status"] = "cancelled"
            if not is_replay:
                await self._leave_work(run_id, reason="provider.event:steer_replacement")
            return

        if event_type == "canvas.action":
            await self._handle_canvas_action(state, payload)
            return

        if event_type in self._PERMISSION_REQUEST_EVENTS:
            permission = self._bounded_permission_request(state, payload)
            if permission.get("diagnosticOnly") is True:
                # This is a retrospective denial, not a live approval handle.
                # Keep it out of pending permissions, but do not hide it: the
                # user must know when validation or another requested action
                # was downgraded.  The dedicated workflow checkpoint has no
                # Allow control and therefore cannot promise an in-place
                # continuation the provider does not support.
                if is_replay:
                    return
                if not self._remember_permission_diagnostic(state, permission):
                    return
                self._accept_semantic_fact(
                    state,
                    semantic_progress_fact(event_type, permission),
                )
                await self._enter_work(run_id, reason=event_type)
                await self._emit_permission_diagnostic_canvas(state, permission)
                return
            self._remember_pending_permission(state, permission)
            if is_replay:
                return
            self._accept_semantic_fact(
                state,
                semantic_progress_fact(event_type, permission),
            )
            await self._enter_work(run_id, reason=event_type)
            await self._emit_permission_canvas(state, permission)
            return

        if event_type in self._PERMISSION_RESOLUTION_EVENTS:
            permission, resolution = self._resolve_pending_permission(state, event_type, payload)
            if is_replay:
                return
            remaining = self._current_pending_permission(state)
            if remaining is not None:
                await self._emit_permission_canvas(state, remaining)
            elif permission is not None:
                await self._emit_permission_resolution_canvas(state, permission, resolution)
            return

        if event_type == "run.status":
            status = str(payload.get("status") or "").strip().lower()
            liveness = str(payload.get("liveness") or "").strip().lower()
            stage = str(payload.get("stage") or "").strip().lower()
            if stage in {"steer_queued", "steer_applied"}:
                state["status"] = status or "running"
                state["steering"] = {
                    "stage": stage,
                    "revision": max(0, int(payload.get("revision") or 0)),
                    "safe_boundary": str(payload.get("safe_boundary") or ""),
                }
                if is_replay:
                    return
                await self._enter_work(run_id, reason=stage)
                await self._emit_steer_canvas(state, payload)
                return
            if liveness in {"stalled", "cancel_pending"} or status == "stalled":
                state["status"] = status or "running"
                state["liveness"] = liveness or "stalled"
                state["liveness_payload"] = dict(payload)
                if is_replay:
                    return
                await self._enter_work(run_id, reason=event_type)
                await self._emit_stalled_canvas(state, payload)
            elif status in self._RUNNING_STATUSES:
                state["status"] = status
                if liveness:
                    state["liveness"] = liveness
                    state["liveness_payload"] = dict(payload)
                if liveness == "active":
                    state["stalled_noted"] = False
                if is_replay:
                    return
                await self._enter_work(run_id, reason=event_type)
                await self._emit_progress_canvas(state, phase="Work", progress=24, force=True)
            elif status in self._TERMINAL_STATUSES:
                state["status"] = status
                state["liveness"] = "terminal"
                if is_replay:
                    return
                if self._steer_closes_semantic_branch(state):
                    # The steer checkpoint already told the truth: this is the
                    # terminal bookkeeping of a superseded plan, not a user-
                    # visible result that should overwrite the new turn.
                    await self._leave_work(run_id, reason=f"{event_type}:semantic_branch_closed")
                    return
                permission = self._current_pending_permission(state)
                if permission is not None:
                    await self._emit_permission_canvas(state, permission, provider_ended=True)
                else:
                    await self._emit_progress_canvas(state, phase="Review", progress=92, force=True)
                await self._leave_work(run_id, reason=f"{event_type}:{status}")
            return

        if event_type == "run.created":
            state["task"] = str(payload.get("task") or state.get("task") or "")
            state["cwd"] = str(payload.get("cwd") or state.get("cwd") or "")
            state["mode"] = str(payload.get("mode") or state.get("mode") or "")
            if is_replay:
                return
            if state.get("intake_emitted"):
                return
            state["intake_emitted"] = True
            await self._enter_work(run_id, reason=event_type)
            await self._emit_progress_canvas(
                state,
                phase="Intake",
                progress=10,
                force=True,
            )
            return

        if event_type == "assistant.delta":
            text = str(payload.get("text") or "")
            if text:
                state["text"] = str(state.get("text") or "") + text
                if is_replay:
                    return
                if self._should_emit_stream_text(str(state.get("text") or "")):
                    await self._emit_progress_canvas(state, phase="Work", progress=68, semantic=False)
            return

        if event_type == "assistant.update":
            text = " ".join(str(payload.get("text") or "").split())
            if text:
                state["text"] = text
                accepted = self._accept_semantic_fact(
                    state,
                    semantic_progress_fact(event_type, payload),
                )
                if is_replay:
                    return
                if not accepted:
                    return
                await self._enter_work(run_id, reason=event_type)
                await self._emit_progress_canvas(
                    state,
                    phase="Work",
                    progress=68,
                    force=True,
                    semantic=True,
                    semantic_candidate=True,
                    narration_keypoint="directional_progress",
                )
            return

        if event_type == "semantic.progress":
            summary = str(payload.get("summary") or payload.get("text") or "").strip()
            if summary:
                accepted = self._accept_semantic_fact(
                    state,
                    semantic_progress_fact(event_type, payload),
                )
                if is_replay:
                    return
                if not accepted:
                    return
                await self._enter_work(run_id, reason=event_type)
                await self._emit_progress_canvas(
                    state,
                    phase="Work",
                    progress=70,
                    force=True,
                    semantic=True,
                    narration_keypoint="semantic_progress",
                )
            return

        if event_type == "tool.call":
            tool = str(payload.get("tool") or "tool")
            tools = state.setdefault("tools", [])
            first_tool = not tools if isinstance(tools, list) else False
            if isinstance(tools, list):
                tools.append(tool)
                state["tools"] = tools[-8:]
            raw_events = state.setdefault("raw_tool_events", [])
            if isinstance(raw_events, list):
                raw_events.append(payload.get("raw") if isinstance(payload.get("raw"), dict) else payload)
                state["raw_tool_events"] = raw_events[-24:]
            state["active_tool_contexts"] = remember_tool_call(
                state.get("active_tool_contexts"), payload
            )
            semantic = self._accept_semantic_fact(
                state,
                semantic_progress_fact(event_type, payload, tool_context=payload),
            )
            if is_replay:
                return
            await self._enter_work(run_id, reason=event_type)
            await self._emit_progress_canvas(
                state,
                phase="Work",
                progress=58 if semantic else 52,
                force=True,
                semantic=semantic,
                narration_keypoint=(
                    "semantic_progress"
                    if semantic
                    else ("first_tool" if first_tool else "")
                ),
            )
            return

        if event_type == "tool.result":
            contexts, tool_context = consume_tool_call(
                state.get("active_tool_contexts"), payload
            )
            state["active_tool_contexts"] = contexts
            semantic = self._accept_semantic_fact(
                state,
                semantic_progress_fact(
                    event_type,
                    payload,
                    tool_context=tool_context,
                ),
            )
            if is_replay:
                return
            await self._enter_work(run_id, reason=event_type)
            await self._emit_progress_canvas(
                state,
                phase="Work",
                progress=66 if semantic else 54,
                force=semantic,
                semantic=semantic,
                narration_keypoint="semantic_progress" if semantic else "",
            )
            return

        if event_type == "artifact.created":
            artifacts = state.setdefault("artifacts", [])
            if isinstance(artifacts, list):
                artifacts.append(payload)
                state["artifacts"] = artifacts[-8:]
            semantic = self._accept_semantic_fact(
                state,
                semantic_progress_fact(event_type, payload),
            )
            artifact_type = str(payload.get("artifact_type") or payload.get("type") or "").strip().lower()
            if artifact_type == "browser.snapshot":
                state["browser_artifact"] = payload
                if is_replay:
                    return
                await self._enter_work(run_id, reason=event_type)
                await self._emit_browser_canvas(
                    state,
                    payload,
                    phase="Preview",
                    progress=82,
                    narration_keypoint=(
                        "semantic_progress" if semantic else "artifact_registered"
                    ),
                )
            else:
                if is_replay:
                    return
                await self._enter_work(run_id, reason=event_type)
                await self._emit_progress_canvas(
                    state,
                    phase="Work",
                    progress=74,
                    force=True,
                    semantic=semantic,
                    narration_keypoint=(
                        "semantic_progress" if semantic else "artifact_registered"
                    ),
                )
            return

        if event_type in self._WORK_START_EVENTS:
            if is_replay:
                return
            await self._enter_work(run_id, reason=event_type)
            await self._emit_progress_canvas(state, phase="Work", progress=44)
            return

        if event_type in self._WORK_TERMINAL_EVENTS:
            state["status"] = event_type.replace("run.", "")
            if payload.get("result"):
                state["result"] = str(payload.get("result") or "")
            if payload.get("error"):
                state["error"] = str(payload.get("error") or "")
            if is_replay:
                return
            browser_artifact = state.get("browser_artifact")
            permission = self._current_pending_permission(state)
            if permission is not None:
                await self._emit_permission_canvas(state, permission, provider_ended=True)
            elif isinstance(browser_artifact, dict):
                await self._emit_browser_canvas(state, browser_artifact, phase="Result", progress=96)
            else:
                await self._emit_progress_canvas(state, phase="Review", progress=92, force=True)

    async def _on_provider_result(self, _method: str, params: dict[str, Any]) -> None:
        run_id = str(params.get("run_id") or "").strip()
        state = self._run_state(params)
        state["status"] = str(params.get("status") or state.get("status") or "")
        state["result"] = str(params.get("result") or state.get("result") or "")
        state["error"] = str(params.get("error") or state.get("error") or "")
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        state["metadata"] = self._merge_run_metadata(state.get("metadata"), metadata)
        metadata = state["metadata"]
        cancellation = (
            metadata.get("cancellation")
            if isinstance(metadata.get("cancellation"), dict)
            else {}
        )
        if str(cancellation.get("reason") or "").strip().lower() == "steer_replacement":
            await self._leave_work(run_id, reason="provider.result:steer_replacement")
            return
        if self._steer_closes_semantic_branch(state):
            await self._leave_work(run_id, reason="provider.result:semantic_branch_closed")
            return
        permission = self._current_pending_permission(state)
        if permission is not None:
            await self._emit_permission_canvas(state, permission, provider_ended=True)
            await self._leave_work(run_id, reason="provider.result:permission_pending")
            return
        browser_metadata = metadata.get("browser") if isinstance(metadata.get("browser"), dict) else {}
        is_browser_run = str(state.get("provider") or "").strip().lower() == "browser"
        metadata_browser_artifact = self._latest_browser_snapshot_from_metadata(metadata)
        browser_artifact = metadata_browser_artifact if metadata_browser_artifact else state.get("browser_artifact")
        if (browser_metadata or is_browser_run) and isinstance(browser_artifact, dict) and browser_artifact:
            state["browser_artifact"] = browser_artifact
            await self._emit_browser_canvas(
                state,
                browser_artifact,
                phase="Result",
                progress=100,
                result_text=state["result"],
            )
            await self._leave_work(run_id, reason="provider.result")
            return
        await self._emit_result_canvas(state)
        await self._leave_work(run_id, reason="provider.result")

    async def _enter_work(self, run_id: str, *, reason: str) -> None:
        if run_id:
            self._active_runs.add(run_id)
            self._ensure_heartbeat_task(run_id)
        await self._emit_behavior_intent(reason=reason, run_id=run_id)

    async def _leave_work(self, run_id: str, *, reason: str) -> None:
        if run_id:
            self._active_runs.discard(run_id)
            self._cancel_heartbeat_task(run_id)
        if self._active_runs:
            return
        state = self._runs.get(run_id) if run_id else None
        if isinstance(state, dict) and state.get("release_owned_by_observer") is True:
            self._schedule_observer_release_fallback(state, run_id=run_id, reason=reason)
            return
        await self._release_presentation(run_id=run_id, reason=reason)

    async def release_work_presentation(self, run_id: str, *, reason: str = "work_observer_terminal") -> None:
        """Release the shared work presentation on behalf of the observer.

        The wallpaper activity and SpriteForge work pose are singletons shared
        by every provider run, so a terminal narration for one run must not
        strip them while a sibling run is still working.
        """
        if self._active_runs:
            return
        await self._release_presentation(run_id=run_id, reason=reason)

    async def _release_presentation(self, *, run_id: str, reason: str) -> None:
        await self._release_behavior_intent(reason=reason, run_id=run_id)

    def _ensure_heartbeat_task(self, run_id: str) -> None:
        if not run_id or run_id in self._heartbeat_tasks:
            return
        interval = self._heartbeat_interval()
        if interval <= 0:
            return
        self._heartbeat_tasks[run_id] = asyncio.create_task(
            self._heartbeat_loop(run_id, interval),
            name=f"work-heartbeat-{run_id}",
        )

    def _cancel_heartbeat_task(self, run_id: str) -> None:
        task = self._heartbeat_tasks.pop(run_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _heartbeat_loop(self, run_id: str, interval: float) -> None:
        try:
            while run_id in self._active_runs:
                await asyncio.sleep(interval)
                if run_id not in self._active_runs:
                    break
                state = self._runs.get(run_id)
                if not isinstance(state, dict):
                    break
                now = time.monotonic()
                await self._maybe_emit_quiet_notice(state, now=now)
                last_canvas_at = float(state.get("last_canvas_at") or 0.0)
                if now - last_canvas_at < interval:
                    continue
                await self._emit_heartbeat_canvas(state, now=now)
        except asyncio.CancelledError:
            return
        except Exception:
            import logging

            logging.getLogger(__name__).debug("provider work heartbeat failed", exc_info=True)
        finally:
            current = asyncio.current_task()
            if self._heartbeat_tasks.get(run_id) is current:
                self._heartbeat_tasks.pop(run_id, None)

    @staticmethod
    def _heartbeat_interval() -> float:
        try:
            return max(0.0, float(getattr(settings, "PROVIDER_WORK_HEARTBEAT_S", 45) or 0))
        except Exception:
            return 45.0

    @staticmethod
    def _quiet_notice_after() -> float:
        try:
            return max(
                0.0,
                float(getattr(settings, "PROVIDER_WORK_QUIET_NOTICE_S", 90) or 0),
            )
        except Exception:
            return 90.0

    @staticmethod
    def _quiet_notice_repeat() -> float:
        try:
            return max(
                0.0,
                float(getattr(settings, "PROVIDER_WORK_QUIET_REPEAT_S", 300) or 0),
            )
        except Exception:
            return 300.0

    async def _maybe_emit_quiet_notice(
        self,
        state: dict[str, Any],
        *,
        now: float,
    ) -> None:
        threshold = self._quiet_notice_after()
        if threshold <= 0 or self._current_pending_permission(state) is not None:
            return
        if str(state.get("status") or "").strip().lower() in self._TERMINAL_STATUSES:
            return
        if str(state.get("liveness") or "").strip().lower() in {
            "stalled",
            "cancel_pending",
            "terminal",
        }:
            return
        last_semantic = float(
            state.get("last_semantic_progress_at")
            or state.get("started_at")
            or now
        )
        last_direction = float(state.get("last_directional_progress_at") or 0.0)
        last_useful_update = max(last_semantic, last_direction)
        semantic_silence_s = max(0.0, now - last_semantic)
        useful_update_silence_s = max(0.0, now - last_useful_update)
        if useful_update_silence_s < threshold:
            return
        last_notice = float(state.get("last_quiet_notice_at") or 0.0)
        repeat = self._quiet_notice_repeat()
        if last_notice > 0 and (repeat <= 0 or now - last_notice < repeat):
            return

        state["last_quiet_notice_at"] = now
        provider_label = self._provider_display_label(
            str(state.get("provider") or "provider")
        )
        duration = self._format_elapsed(useful_update_silence_s)
        provider_silence_s = max(
            0.0,
            now
            - float(
                state.get("last_provider_activity_at")
                or state.get("started_at")
                or now
            ),
        )
        if provider_silence_s < useful_update_silence_s:
            summary = (
                f"No new meaningful progress has been reported for {duration}. "
                "Provider events are still arriving; Amadeus is monitoring the run."
            )
            detail = "Provider activity continues; no completion or failure was reported"
        else:
            summary = (
                f"No new meaningful progress or provider event has arrived for {duration}. "
                "The task has not reported completion; Amadeus is still monitoring it."
            )
            detail = "No completion or failure has been reported"
        await self._emit_work_note(
            state,
            phase="Work",
            title=f"{provider_label} has no new milestone yet",
            summary=summary,
            signals=[
                work_signal(
                    label="heartbeat",
                    text=f"No meaningful progress for {duration}",
                    detail=detail,
                    kind="status",
                )
            ],
            importance="normal",
            metadata_extra={
                "narration_keypoint": "quiet_monitoring",
                "silence_s": round(useful_update_silence_s, 1),
                "semantic_silence_s": round(semantic_silence_s, 1),
                "useful_update_silence_s": round(useful_update_silence_s, 1),
                "provider_silence_s": round(provider_silence_s, 1),
                **(
                    {
                        "directional_summary": str(
                            state.get("semantic_candidate_text") or ""
                        )[:320],
                        "directional_source": str(
                            state.get("semantic_candidate_source") or ""
                        )[:80],
                    }
                    if str(state.get("semantic_candidate_text") or "").strip()
                    else {}
                ),
                "user_action_required": False,
            },
        )

    async def _emit_heartbeat_canvas(self, state: dict[str, Any], *, now: float) -> None:
        permission = self._current_pending_permission(state)
        if permission is not None:
            await self._emit_permission_canvas(state, permission)
            return
        provider = str(state.get("provider") or "provider")
        provider_label = self._provider_display_label(provider)
        progress = int(state.get("last_progress") or 24)
        lead, has_provider_lead = self._current_progress_lead(state)
        elapsed_s = max(0.0, now - float(state.get("started_at") or now))
        liveness = str(state.get("liveness") or "active")
        liveness_payload = (
            state.get("liveness_payload")
            if isinstance(state.get("liveness_payload"), dict)
            else {}
        )
        silence_s = float(liveness_payload.get("silence_s") or 0.0)
        observed_at = float(liveness_payload.get("observed_at") or 0.0)
        if observed_at > 0:
            silence_s += max(0.0, time.time() - observed_at)
        if liveness == "stalled":
            signal_text = f"Provider quiet for {self._format_elapsed(silence_s)}"
            signal_detail = "Amadeus is still monitoring"
            if liveness_payload.get("probe_status"):
                signal_detail += f"; {provider_label} reports {liveness_payload['probe_status']}"
        elif liveness == "cancel_pending":
            signal_text = "Stopping..."
            signal_detail = f"Waiting for {provider_label} to confirm cancellation"
        else:
            signal_text = f"{self._format_elapsed(elapsed_s)} elapsed"
            signal_detail = str(state.get("status") or "running")
        state["last_canvas_at"] = now
        state["last_progress"] = progress
        await bus.emit(
            Method.WALLPAPER_CANVAS,
            canvas_payload(
                mode="workflow",
                phase="Work",
                title=f"{provider_label} work signal",
                lead=lead,
                progress=progress,
                signals=[
                    work_signal(
                        label="elapsed",
                        text=signal_text,
                        detail=signal_detail,
                        kind="status",
                        presentation={
                            "text": presentation_message(
                                "heartbeat.quiet" if liveness == "stalled" else
                                "heartbeat.stopping" if liveness == "cancel_pending" else
                                "heartbeat.elapsed",
                                duration=(
                                    self._format_elapsed(silence_s)
                                    if liveness == "stalled"
                                    else self._format_elapsed(elapsed_s)
                                ),
                            ),
                            "detail": presentation_message(
                                "heartbeat.waiting_cancel"
                                if liveness == "cancel_pending"
                                else "heartbeat.monitoring"
                            ),
                        },
                    )
                ],
                size_preset="compact",
                open=True,
                metadata=self._canvas_metadata(state),
                presentation={
                    "title": presentation_message("provider.work_signal", provider=provider_label),
                    **(
                        {}
                        if has_provider_lead
                        else {"lead": presentation_message("provider.executing_selected_task")}
                    ),
                },
            ),
        )

    def _run_state(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "").strip() or "provider"
        state = self._runs.setdefault(
            run_id,
            {
                "run_id": run_id,
                "provider": str(params.get("provider") or "provider"),
                "task": str(params.get("task") or ""),
                "cwd": str(params.get("cwd") or ""),
                "status": "",
                "text": "",
                "result": "",
                "error": "",
                "tools": [],
                "artifacts": [],
                "metadata": {},
                "last_canvas_at": 0.0,
                "last_progress": 0,
                "started_at": time.monotonic(),
                "last_provider_activity_at": time.monotonic(),
                "last_semantic_progress_at": time.monotonic(),
                "last_directional_progress_at": 0.0,
                "last_semantic_fact_key": "",
                "recent_semantic_fact_keys": [],
                "recent_semantic_candidate_keys": [],
                "progress_lead": "",
                "active_tool_contexts": {},
                "last_quiet_notice_at": 0.0,
                "mode": "",
                "pending_permissions": {},
                "liveness": "active",
                "liveness_payload": {},
                "stalled_noted": False,
            },
        )
        if params.get("provider"):
            state["provider"] = str(params.get("provider") or "")
        if params.get("task"):
            state["task"] = str(params.get("task") or "")
        if params.get("cwd"):
            state["cwd"] = str(params.get("cwd") or "")
        return state

    @staticmethod
    def _accept_semantic_fact(
        state: dict[str, Any],
        fact: SemanticProgressFact | None,
    ) -> bool:
        """Record a semantic signal; only evidence-grade facts reset the clock."""

        if fact is not None and fact.evidence == "candidate":
            seen_candidates = state.get("recent_semantic_candidate_keys")
            if not isinstance(seen_candidates, list):
                seen_candidates = []
            if fact.key in seen_candidates:
                return False
            seen_candidates.append(fact.key)
            state["recent_semantic_candidate_keys"] = seen_candidates[-24:]
            state["semantic_candidate_text"] = fact.summary
            state["semantic_candidate_source"] = fact.source
            state["last_directional_progress_at"] = time.monotonic()
            state["last_quiet_notice_at"] = 0.0
            return True

        seen = state.get("recent_semantic_fact_keys")
        if not isinstance(seen, list):
            seen = []
        if fact is None or fact.key in seen:
            return False
        seen.append(fact.key)
        state["recent_semantic_fact_keys"] = seen[-24:]
        state["last_semantic_fact_key"] = fact.key
        state["last_semantic_progress_at"] = time.monotonic()
        state["last_quiet_notice_at"] = 0.0
        state["semantic_text"] = fact.summary
        state["semantic_source"] = fact.source
        state["semantic_explicit"] = fact.explicit
        state["semantic_verified"] = fact.verified
        state["semantic_milestone"] = fact.milestone
        state["semantic_evidence"] = fact.evidence
        return True

    @staticmethod
    def _monotonic_progress(state: dict[str, Any], progress: int) -> int:
        """Clamp the workflow bar so it never moves backwards within a run.

        Live events carry coarse per-type progress hints (tool=52, delta=68,
        artifact=74, ...); replaying them verbatim makes the bar saw-tooth.
        The per-run state is the clamp boundary: a retry is a new run_id and
        starts from 0 again.
        """
        try:
            last = int(state.get("last_progress") or 0)
        except (TypeError, ValueError):
            last = 0
        clamped = max(int(progress), last)
        state["last_progress"] = clamped
        return clamped

    def _current_progress_lead(
        self,
        state: dict[str, Any],
        text: str = "",
        *,
        limit: int = 120,
    ) -> tuple[str, bool]:
        """Keep a useful provider lead across mechanical canvas refreshes.

        Semantic milestones and bounded provider updates are event-driven, while
        status and heartbeat canvases are periodic.  Remembering the last lead
        in per-run presentation state prevents a refresh from replacing useful
        context with the generic fallback.  The boolean tells callers whether
        the lead came from the provider; only the fallback is statically
        localized.
        """

        candidate = self._trim(text, limit)
        if candidate:
            state["progress_lead"] = candidate
            return candidate, True
        remembered = self._trim(str(state.get("progress_lead") or ""), limit)
        if remembered:
            return remembered, True
        task = self._trim(str(state.get("task") or "Provider task"), limit)
        return task, False

    @staticmethod
    def _merge_run_metadata(current: Any, incoming: Any) -> dict[str, Any]:
        """Merge adapter telemetry without dropping the stable work binding.

        Provider adapters are allowed to emit sparse event metadata (for
        example only a native run id). Replacing the request metadata with
        those sparse dictionaries used to erase the WorkItem identity before
        progress and heartbeat canvases were rendered.
        """
        merged = dict(current) if isinstance(current, dict) else {}
        update = dict(incoming) if isinstance(incoming, dict) else {}
        current_work = merged.get("work") if isinstance(merged.get("work"), dict) else {}
        incoming_work = update.get("work") if isinstance(update.get("work"), dict) else {}
        merged.update(update)
        if current_work or incoming_work:
            merged["work"] = {**current_work, **incoming_work}
        return merged

    @staticmethod
    def _canvas_metadata(state: dict[str, Any], **extra: Any) -> dict[str, Any]:
        request_metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        work = request_metadata.get("work") if isinstance(request_metadata.get("work"), dict) else {}
        metadata: dict[str, Any] = {
            "provider": str(state.get("provider") or "provider"),
            "run_id": str(state.get("run_id") or ""),
            "cwd": str(state.get("cwd") or ""),
        }
        if work:
            metadata["work"] = dict(work)
        session_id = str(request_metadata.get("session_id") or "").strip()
        if session_id:
            metadata["session_id"] = session_id
        metadata.update({key: value for key, value in extra.items() if value not in (None, "")})
        return metadata

    def _bounded_permission_request(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Project provider permission data into a small, non-secret UI contract.

        Provider event payloads are not forwarded wholesale. In particular,
        tool input/content, environment values, credentials, and arbitrary
        metadata never enter the canvas permission model.
        """
        source: dict[str, Any] = {}
        for key in ("permissionRequest", "permission_request", "request", "permission"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                source.update(nested)
                break
        source.update(payload)

        raw_id = self._first_permission_scalar(
            source,
            (
                "id",
                "request_id",
                "requestId",
                "permission_id",
                "permissionId",
                "toolUseId",
                "tool_use_id",
            ),
        )
        request_id = self._normalize_permission_id(raw_id)
        if not request_id:
            run_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(state.get("run_id") or "provider"))[:72]
            request_id = f"permission:{run_id or 'provider'}"

        capability = self._safe_permission_text(
            self._first_permission_scalar(source, ("capability", "resource_type", "resourceType", "kind")),
            96,
        )
        permission_value = source.get("permission")
        if not capability and not isinstance(permission_value, (dict, list, tuple, set)):
            capability = self._safe_permission_text(permission_value, 96)
        action = self._safe_permission_text(
            self._first_permission_scalar(source, ("action", "operation", "tool", "tool_name", "toolName")),
            96,
        )
        tool = self._safe_permission_text(
            self._first_permission_scalar(source, ("toolName", "tool_name", "tool")),
            96,
        )
        reason = self._safe_permission_text(
            self._first_permission_scalar(source, ("reason", "message", "summary", "detail")),
            320,
        )

        reversibility_value = source.get("reversibility")
        if reversibility_value in (None, ""):
            reversibility_value = source.get("reversible")
        if isinstance(reversibility_value, bool):
            reversibility = "reversible" if reversibility_value else "irreversible"
        else:
            reversibility = self._safe_permission_text(reversibility_value, 64)

        scope_value: Any = None
        for key in ("scope", "scopes", "paths", "path", "resources", "resource", "targets", "target"):
            if source.get(key) not in (None, ""):
                scope_value = source.get(key)
                break
        scope = self._bounded_permission_list(
            scope_value,
            limit=self._MAX_PERMISSION_SCOPE,
            item_limit=180,
            allow_option_dict=False,
        )

        diagnostic_only = source.get("diagnosticOnly") is True or source.get(
            "diagnostic_only"
        ) is True
        option_value = source.get("options")
        if option_value in (None, ""):
            option_value = source.get("choices")
        options = (
            ["deny"]
            if diagnostic_only
            else self._bounded_permission_list(
                option_value,
                limit=self._MAX_PERMISSION_OPTIONS,
                item_limit=64,
                allow_option_dict=True,
            )
        )
        if not options:
            options = ["deny"]
        return {
            "id": request_id,
            "capability": capability,
            "action": action,
            "tool": tool,
            "scope": scope,
            "reason": reason,
            "reversibility": reversibility,
            "options": options,
            "diagnosticOnly": diagnostic_only,
            "retryRequired": source.get("retryRequired") is True
            or source.get("retry_required") is True,
        }

    @staticmethod
    def _first_permission_scalar(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = source.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list, tuple, set)):
                return value
        return ""

    def _bounded_permission_list(
        self,
        value: Any,
        *,
        limit: int,
        item_limit: int,
        allow_option_dict: bool,
    ) -> list[str]:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            candidate: Any = item
            if isinstance(item, dict):
                candidate = self._first_permission_scalar(
                    item,
                    ("id", "action", "value", "label")
                    if allow_option_dict
                    else ("path", "resource", "target", "uri"),
                )
            text = self._safe_permission_text(candidate, item_limit)
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
            if len(result) >= limit:
                break
        return result

    def _safe_permission_text(self, value: Any, limit: int) -> str:
        if value in (None, "") or isinstance(value, (dict, list, tuple, set)):
            return ""
        text = " ".join(str(value).split())
        text = self._SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
        text = self._BEARER_SECRET_RE.sub("Bearer [redacted]", text)
        text = self._KEY_SECRET_RE.sub("[redacted]", text)
        return self._trim(text, limit)

    def _normalize_permission_id(self, value: Any) -> str:
        text = self._safe_permission_text(value, 160)
        return re.sub(r"[^A-Za-z0-9_.:-]+", "-", text).strip("-:")[:96]

    def _remember_pending_permission(self, state: dict[str, Any], permission: dict[str, Any]) -> None:
        pending = state.get("pending_permissions")
        if not isinstance(pending, dict):
            pending = {}
            state["pending_permissions"] = pending
        request_id = str(permission.get("id") or "")
        pending[request_id] = dict(permission)
        while len(pending) > self._MAX_PENDING_PERMISSIONS:
            pending.pop(next(iter(pending)))

    def _remember_permission_diagnostic(
        self,
        state: dict[str, Any],
        permission: dict[str, Any],
    ) -> bool:
        """Remember one semantic denial while coalescing repeated tool retries."""

        material = json.dumps(
            {
                "capability": permission.get("capability"),
                "action": permission.get("action"),
                "scope": permission.get("scope"),
                "reason": permission.get("reason"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).casefold()
        seen = state.get("permission_diagnostic_keys")
        if not isinstance(seen, set):
            seen = set()
            state["permission_diagnostic_keys"] = seen
        state["permission_diagnostic_count"] = int(
            state.get("permission_diagnostic_count") or 0
        ) + 1
        state["last_permission_diagnostic"] = dict(permission)
        if material in seen:
            return False
        seen.add(material)
        return True

    @staticmethod
    def _current_pending_permission(state: dict[str, Any]) -> dict[str, Any] | None:
        pending = state.get("pending_permissions")
        if not isinstance(pending, dict) or not pending:
            return None
        value = next(reversed(pending.values()))
        return dict(value) if isinstance(value, dict) else None

    def _resolve_pending_permission(
        self,
        state: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        pending = state.get("pending_permissions")
        if not isinstance(pending, dict):
            pending = {}
            state["pending_permissions"] = pending
        source = payload
        for key in ("permissionRequest", "permission_request", "request", "permission"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                source = {**nested, **payload}
                break
        request_id = self._normalize_permission_id(
            self._first_permission_scalar(
                source,
                (
                    "id",
                    "request_id",
                    "requestId",
                    "permission_id",
                    "permissionId",
                    "toolUseId",
                    "tool_use_id",
                ),
            )
        )
        matched_id = request_id if request_id in pending else ""
        if not matched_id and not request_id and pending:
            matched_id = next(reversed(pending))
        permission = pending.pop(matched_id, None) if matched_id else None

        raw_status = self._safe_permission_text(
            self._first_permission_scalar(source, ("status", "decision", "outcome", "result")),
            32,
        ).lower()
        if event_type in {"permission.denied", "permission.rejected"}:
            status = "denied"
        elif event_type == "permission.expired":
            status = "expired"
        elif event_type in {"permission.allowed", "permission.approved", "permission.granted"}:
            status = "allowed"
        elif raw_status in {"denied", "rejected", "declined"}:
            status = "denied"
        elif raw_status in {"expired", "timed_out", "timeout"}:
            status = "expired"
        elif raw_status in {"allowed", "approved", "granted", "accepted", "resolved"}:
            status = "allowed"
        else:
            status = "resolved"
        reason = self._safe_permission_text(
            self._first_permission_scalar(source, ("reason", "message", "summary", "detail")),
            240,
        )
        return (dict(permission) if isinstance(permission, dict) else None, {"status": status, "reason": reason})

    async def _emit_permission_canvas(
        self,
        state: dict[str, Any],
        permission: dict[str, Any],
        *,
        provider_ended: bool = False,
    ) -> None:
        provider_label = self._provider_display_label(str(state.get("provider") or "provider"))
        action = str(permission.get("action") or "").strip()
        capability = str(permission.get("capability") or "").strip()
        reason = str(permission.get("reason") or "").strip()
        request_text = reason or (
            f"{action} was blocked by policy."
            if action
            else f"{capability} was blocked by policy."
            if capability
            else "The provider action was blocked by policy."
        )
        title = f"{provider_label} action blocked"
        lead = request_text
        detail_parts = [str(item) for item in (permission.get("scope") or [])[:2] if str(item).strip()]
        reversibility = str(permission.get("reversibility") or "").strip()
        if reversibility:
            detail_parts.append(reversibility)
        signals = [
            work_signal(
                label="permission",
                text=request_text,
                detail="; ".join(detail_parts),
                kind="permission",
                importance="blocking",
                ref=str(permission.get("id") or ""),
                presentation={
                    "text": presentation_message("permission.provider_blocked"),
                },
            )
        ]
        if provider_ended:
            signals.append(
                work_signal(
                    label="checkpoint",
                    text="The stopped run cannot accept an in-place policy grant.",
                    detail=str(state.get("status") or "ended"),
                    kind="status",
                    importance="blocking",
                    presentation={
                        "text": presentation_message("permission.stopped_run"),
                    },
                )
            )
        canvas = canvas_payload(
            mode="permission",
            phase="Checkpoint",
            title=title,
            lead=lead,
            progress=int(state.get("last_progress") or 72),
            signals=signals,
            size_preset="compact",
            open=True,
            metadata=self._canvas_metadata(
                state,
                permission_request_id=str(permission.get("id") or ""),
                attention="permission",
            ),
            presentation={
                "title": presentation_message("provider.action_blocked", provider=provider_label),
                "lead": presentation_message("permission.provider_blocked"),
                "permissionRequest.reason": presentation_message("permission.provider_blocked"),
            },
        )
        canvas["permissionVisible"] = True
        canvas["permissionRequest"] = dict(permission)
        state["last_canvas_at"] = time.monotonic()
        state["last_progress"] = int(canvas.get("progress") or 72)
        await bus.emit(Method.WALLPAPER_CANVAS, canvas)
        await self._emit_work_note(
            state,
            phase="Checkpoint",
            title=title,
            summary=lead,
            signals=signals,
            importance="blocking",
            observer_policy="auto",
            metadata_extra={
                "permission_request_id": str(permission.get("id") or ""),
                "permission_issue": {
                    "capability": str(permission.get("capability") or ""),
                    "action": str(permission.get("action") or ""),
                    "scope": [
                        str(item)
                        for item in (permission.get("scope") or [])[:4]
                        if str(item).strip()
                    ],
                },
                "permission_status": "pending",
                "attention": "permission",
                "narration_keypoint": "permission_pending",
            },
        )

    async def _emit_permission_diagnostic_canvas(
        self,
        state: dict[str, Any],
        permission: dict[str, Any],
    ) -> None:
        """Expose a denial without presenting a decision the run cannot accept."""

        provider_label = self._provider_display_label(
            str(state.get("provider") or "provider")
        )
        tool = str(permission.get("tool") or "").strip()
        capability = str(permission.get("capability") or "provider action").strip()
        action = str(permission.get("action") or "invoke").strip()
        reason = str(permission.get("reason") or "").strip()
        operation = tool or f"{capability}: {action}"
        lead = reason or f"{operation} was denied by provider policy."
        scope = [str(item) for item in (permission.get("scope") or [])[:2] if str(item)]
        signal = work_signal(
            label="permission",
            text=f"{operation} was denied; this run cannot approve it in place.",
            detail="; ".join(scope) or "The run may continue with a narrower alternative.",
            kind="permission",
            importance="important",
            ref=str(permission.get("id") or ""),
            presentation={
                "text": presentation_message("permission.denied_operation", operation=operation),
                "detail": presentation_message("permission.narrower_alternative"),
            },
        )
        canvas = canvas_payload(
            mode="workflow",
            phase="Checkpoint",
            title=f"{provider_label} action blocked",
            lead=lead,
            progress=int(state.get("last_progress") or 52),
            signals=[signal],
            size_preset="compact",
            open=True,
            metadata=self._canvas_metadata(
                state,
                permission_request_id=str(permission.get("id") or ""),
                permission_diagnostic=True,
                attention="review",
            ),
            presentation={
                "title": presentation_message("provider.action_blocked", provider=provider_label),
                "lead": presentation_message("permission.provider_blocked"),
            },
        )
        canvas["blocking"] = False
        canvas["permissionVisible"] = False
        state["last_canvas_at"] = time.monotonic()
        await bus.emit(Method.WALLPAPER_CANVAS, canvas)
        await self._emit_work_note(
            state,
            phase="Checkpoint",
            title=f"{provider_label} action blocked",
            summary=(
                f"{operation} was denied by provider policy. The current run cannot "
                "approve that call in place and may continue with an alternative."
            ),
            signals=[signal],
            importance="important",
            observer_policy="auto",
            metadata_extra={
                "permission_request_id": str(permission.get("id") or ""),
                "permission_issue": {
                    "capability": str(permission.get("capability") or ""),
                    "action": str(permission.get("action") or ""),
                    "scope": [
                        str(item)
                        for item in (permission.get("scope") or [])[:4]
                        if str(item).strip()
                    ],
                },
                "permission_status": "denied",
                "permission_actionable": False,
                "permission_diagnostic": True,
                "permission_retry_required": bool(permission.get("retryRequired")),
                "attention": "review",
                "narration_keypoint": "permission_blocked",
            },
        )

    async def _emit_permission_resolution_canvas(
        self,
        state: dict[str, Any],
        permission: dict[str, Any],
        resolution: dict[str, str],
    ) -> None:
        status = str(resolution.get("status") or "resolved")
        denied = status in {"denied", "expired"}
        reason = str(resolution.get("reason") or "").strip()
        title = "Permission denied" if denied else "Permission resolved"
        lead = reason or (
            "The provider cannot continue with this operation."
            if denied
            else "The provider reported the checkpoint resolved; retry is still a separate run."
        )
        signal = work_signal(
            label="permission",
            text=lead,
            detail=status,
            kind="permission",
            importance="blocking" if denied else "normal",
            ref=str(permission.get("id") or ""),
            presentation={
                "text": presentation_message(
                    "permission.denied_lead" if denied else "permission.resolved_lead"
                ),
            },
        )
        canvas = canvas_payload(
            mode="workflow",
            phase="Checkpoint",
            title=title,
            lead=lead,
            progress=int(state.get("last_progress") or 72),
            signals=[signal],
            size_preset="compact",
            open=True,
            metadata=self._canvas_metadata(
                state,
                permission_request_id=str(permission.get("id") or ""),
                permission_status=status,
            ),
            presentation={
                "title": presentation_message(
                    "permission.denied_title" if denied else "permission.resolved_title"
                ),
                "lead": presentation_message(
                    "permission.denied_lead" if denied else "permission.resolved_lead"
                ),
            },
        )
        canvas["permissionVisible"] = False
        canvas["permissionRequest"] = dict(permission)
        canvas["permissionResolution"] = {
            "id": str(permission.get("id") or ""),
            "status": status,
            "reason": reason,
        }
        state["last_canvas_at"] = time.monotonic()
        await bus.emit(Method.WALLPAPER_CANVAS, canvas)

    async def _emit_progress_canvas(
        self,
        state: dict[str, Any],
        *,
        phase: str,
        progress: int,
        force: bool = False,
        semantic: bool = False,
        semantic_candidate: bool = False,
        narration_keypoint: str = "",
    ) -> None:
        permission = self._current_pending_permission(state)
        if permission is not None:
            await self._emit_permission_canvas(state, permission)
            return
        now = time.monotonic()
        if not force and now - float(state.get("last_canvas_at") or 0.0) < 0.85:
            return
        state["last_canvas_at"] = now
        progress = self._monotonic_progress(state, progress)
        provider = str(state.get("provider") or "provider")
        provider_label = self._provider_display_label(provider)
        source_text = str(
            state.get("semantic_candidate_text")
            if semantic_candidate
            else state.get("semantic_text")
            if semantic
            else state.get("text") or ""
        )
        text = self._trim(source_text, 120) if semantic else self._trim_tail(source_text, 120)
        lead, has_provider_lead = self._current_progress_lead(state, text)
        signals = self._progress_signals(
            state,
            phase=phase,
            text=text,
            semantic=semantic,
            semantic_candidate=semantic_candidate,
        )
        await bus.emit(
            Method.WALLPAPER_CANVAS,
            canvas_payload(
                mode="workflow",
                phase=phase,
                title=f"{provider_label} work signal",
                lead=lead,
                progress=progress,
                signals=signals,
                size_preset="compact",
                open=True,
                metadata=self._canvas_metadata(state),
                presentation={
                    "title": presentation_message("provider.work_signal", provider=provider_label),
                    **(
                        {}
                        if has_provider_lead
                        else {"lead": presentation_message("provider.executing_selected_task")}
                    ),
                },
            ),
        )
        await self._emit_work_note(
            state,
            phase=phase,
            title=f"{provider_label} work signal",
            summary=text or self._trim(str(state.get("task") or "Provider task"), 110),
            signals=signals,
            importance="normal",
            metadata_extra=(
                {
                    "narration_keypoint": narration_keypoint,
                    **(
                        {
                            **(
                                {
                                    "semantic_milestone": str(
                                        state.get("semantic_milestone") or ""
                                    )
                                }
                                if state.get("semantic_milestone")
                                else {}
                            ),
                            "semantic_source": str(state.get("semantic_source") or ""),
                            "semantic_verified": state.get("semantic_verified") is True,
                            "semantic_evidence": str(
                                state.get("semantic_evidence") or "reported"
                            ),
                        }
                        if semantic and not semantic_candidate
                        else {}
                    ),
                    **(
                        {
                            "semantic_candidate": True,
                            "directional_update": True,
                            "semantic_source": str(
                                state.get("semantic_candidate_source")
                                or "provider_assistant_update"
                            ),
                        }
                        if semantic_candidate
                        else {}
                    ),
                }
                if narration_keypoint
                else None
            ),
        )

    async def _emit_steer_canvas(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Expose steer receipt/application without adding controls or speech."""

        stage = str(payload.get("stage") or "").strip().lower()
        revision = max(0, int(payload.get("revision") or 0))
        closing_branch = self._steer_closes_semantic_branch(state)
        if stage == "steer_applied":
            # The previous lead belongs to the superseded plan.  Let the
            # replanned branch establish its own current semantic fact.
            state["progress_lead"] = ""
        if closing_branch and stage == "steer_applied":
            summary = (
                "The remaining browser plan stopped; the browser session is still available."
            )
            detail = "stopped at safe boundary"
        elif closing_branch:
            summary = (
                "Stop requested; waiting for the current browser action to reach a safe boundary."
            )
            detail = str(payload.get("safe_boundary") or "next atomic boundary")
        elif stage == "steer_applied":
            summary = (
                "The new instruction is active; Browser replanned from the current page."
            )
            detail = "applied at safe boundary"
        else:
            summary = (
                "New instruction received; waiting for the current browser action to "
                "reach a safe boundary."
            )
            detail = str(payload.get("safe_boundary") or "next atomic boundary")
        progress = self._monotonic_progress(
            state,
            max(24, int(state.get("last_progress") or 0)),
        )
        signals = [
            work_signal(
                label="instruction",
                text=summary,
                detail=f"revision {revision}: {detail}",
                kind="status",
                importance="important",
                presentation={
                    "text": presentation_message(
                        "browser.plan_stopped"
                        if closing_branch and stage == "steer_applied"
                        else "browser.stop_waiting"
                        if closing_branch
                        else "browser.replanned"
                        if stage == "steer_applied"
                        else "browser.instruction_waiting"
                    ),
                    "detail": presentation_message(
                        "browser.revision",
                        revision=revision,
                        detail=detail,
                    ),
                },
            ),
            work_signal(
                label="task",
                text=self._trim(str(state.get("task") or "Browser task"), 100),
                detail="same provider run",
                kind="run",
                presentation={
                    "text": presentation_message("task.selected"),
                    "detail": presentation_message("task.same_run"),
                },
            ),
        ]
        metadata = self._canvas_metadata(
            state,
            steering_stage=stage,
            steering_revision=revision,
        )
        await bus.emit(
            Method.WALLPAPER_CANVAS,
            canvas_payload(
                mode="workflow",
                phase="Checkpoint",
                title="Browser instruction updated",
                lead=summary,
                progress=progress,
                signals=signals,
                size_preset="compact",
                open=True,
                metadata=metadata,
                presentation={
                    "title": presentation_message("browser.instruction_updated"),
                    "lead": presentation_message(
                        "browser.plan_stopped"
                        if closing_branch and stage == "steer_applied"
                        else "browser.stop_waiting"
                        if closing_branch
                        else "browser.replanned"
                        if stage == "steer_applied"
                        else "browser.instruction_waiting"
                    ),
                },
            ),
        )
        await self._emit_work_note(
            state,
            phase="Checkpoint",
            title="Browser instruction updated",
            summary=summary,
            signals=signals,
            importance="normal",
            observer_policy="silent",
            metadata_extra={
                "steering_stage": stage,
                "steering_revision": revision,
            },
        )

    @staticmethod
    def _steer_closes_semantic_branch(state: dict[str, Any]) -> bool:
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        control = str(metadata.get("branch_control") or "").strip().lower()
        return control in {"close", "supersede", "stop_plan"}

    @staticmethod
    def _is_steer_replacement_cancellation(
        state: dict[str, Any],
        event_type: str,
    ) -> bool:
        if event_type != "run.cancelled":
            return False
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        cancellation = (
            metadata.get("cancellation")
            if isinstance(metadata.get("cancellation"), dict)
            else {}
        )
        return str(cancellation.get("reason") or "").strip().lower() == "steer_replacement"

    async def _emit_stalled_canvas(self, state: dict[str, Any], payload: dict[str, Any]) -> None:
        provider = str(state.get("provider") or "provider")
        provider_label = self._provider_display_label(provider)
        progress = int(state.get("last_progress") or 24)
        lead, has_provider_lead = self._current_progress_lead(state)
        detail_parts = []
        stage = payload.get("stage")
        elapsed = payload.get("elapsed_s")
        silence = payload.get("silence_s")
        liveness = str(payload.get("liveness") or "stalled").strip().lower()
        if stage not in (None, ""):
            detail_parts.append(f"stage={stage}")
        if elapsed not in (None, ""):
            detail_parts.append(f"elapsed_s={elapsed}")
        if payload.get("probe_status") not in (None, ""):
            detail_parts.append(f"probe={payload.get('probe_status')}")
        if liveness == "cancel_pending":
            heartbeat_text = f"Stopping... waiting for {provider_label} confirmation"
            summary = "Cancellation was requested but the provider has not confirmed it yet."
        elif str(stage or "") == "create":
            heartbeat_text = "No provider events yet (create phase)"
            summary = "No provider events have arrived yet; Amadeus is still monitoring."
        else:
            quiet_s = float(silence or 0.0)
            heartbeat_text = f"Provider quiet for {self._format_elapsed(quiet_s)}"
            probe = str(payload.get("probe_status") or "unknown").strip()
            summary = (
                f"No provider updates have arrived for {self._format_elapsed(quiet_s)}. "
                f"The liveness probe reports {probe}; Amadeus is still monitoring."
            )
        signals = self._progress_signals(state, phase="Work", text="", semantic=False)
        signals.append(
            work_signal(
                label="heartbeat",
                text=heartbeat_text,
                detail=", ".join(detail_parts),
                kind="status",
                presentation={
                    "text": presentation_message(
                        "heartbeat.stopping"
                        if liveness == "cancel_pending"
                        else "heartbeat.no_events"
                        if str(stage or "") == "create"
                        else "heartbeat.quiet",
                        duration=self._format_elapsed(float(silence or 0.0)),
                    ),
                    "detail": presentation_message("heartbeat.monitoring"),
                },
            )
        )
        state["last_canvas_at"] = time.monotonic()
        state["last_progress"] = progress
        await bus.emit(
            Method.WALLPAPER_CANVAS,
            canvas_payload(
                mode="workflow",
                phase="Work",
                title=f"{provider_label} work signal",
                lead=lead,
                progress=progress,
                signals=signals[:5],
                size_preset="compact",
                open=True,
                metadata=self._canvas_metadata(state),
                presentation={
                    "title": presentation_message("provider.work_signal", provider=provider_label),
                    **(
                        {}
                        if has_provider_lead
                        else {"lead": presentation_message("provider.executing_selected_task")}
                    ),
                },
            ),
        )
        if not state.get("stalled_noted"):
            state["stalled_noted"] = True
            await self._emit_work_note(
                state,
                phase="Work",
                title=f"{provider_label} work stalled",
                summary=summary,
                signals=signals[:5],
                importance="important",
                metadata_extra={"narration_keypoint": "stalled"},
            )

    async def _emit_browser_canvas(
        self,
        state: dict[str, Any],
        artifact: dict[str, Any],
        *,
        phase: str,
        progress: int,
        result_text: str = "",
        narration_keypoint: str = "",
    ) -> None:
        progress = self._monotonic_progress(state, progress)
        provider = str(state.get("provider") or "browser")
        provider_label = self._provider_display_label(provider)
        title = self._trim(str(artifact.get("title") or "Browser snapshot"), 120)
        url = str(artifact.get("url") or "").strip()
        excerpt = self._trim(str(artifact.get("excerpt") or result_text or state.get("task") or ""), 360)
        links = artifact.get("links") if isinstance(artifact.get("links"), list) else []
        screenshot = str(artifact.get("screenshot") or "")
        signals = [
            work_signal(label="source", text=title, detail=self._host_label(url), kind="source"),
            work_signal(
                label="engine",
                text=str(artifact.get("engine") or "browser"),
                detail=str(artifact.get("status_code") or ""),
                kind="browser",
            ),
        ]
        if links:
            signals.append(
                work_signal(
                    label="links",
                    text=f"{len(links)} navigable source link(s)",
                    detail="chip actions",
                    kind="source",
                    presentation={
                        "text": presentation_message("links.navigable", count=len(links)),
                        "detail": presentation_message("links.actions"),
                    },
                )
            )
        await bus.emit(
            Method.WALLPAPER_CANVAS,
            browser_canvas_payload(
                phase=phase,
                title=title,
                excerpt=excerpt,
                url=url,
                browser_session_id=str(artifact.get("browser_session_id") or ""),
                links=links[:6],
                screenshot=screenshot,
                signals=signals,
                progress=progress,
                result_text=result_text,
                size_preset="wide" if screenshot else "compact",
                metadata=self._canvas_metadata(state, artifact_type="browser.snapshot"),
            ),
        )
        await self._emit_work_note(
            state,
            phase=phase,
            title=f"{provider_label} source preview",
            summary=excerpt or title,
            signals=signals,
            importance="normal" if phase.lower() != "result" else "important",
            observer_policy="silent",
            metadata_extra={
                "continuable": True,
                "provider_context_kind": "browser.snapshot",
                "browser_session_id": str(artifact.get("browser_session_id") or ""),
                "url": url,
                "page_title": title,
                **({"narration_keypoint": narration_keypoint} if narration_keypoint else {}),
            },
        )

    async def _emit_result_canvas(self, state: dict[str, Any]) -> None:
        provider = str(state.get("provider") or "provider")
        provider_label = self._provider_display_label(provider)
        task = self._trim(str(state.get("task") or "Provider task"), 160)
        status = str(state.get("status") or "done")
        result = str(state.get("result") or state.get("error") or "").strip()
        tools = [str(item) for item in (state.get("tools") or []) if str(item).strip()]
        source_actions = self._source_actions(state, result)
        source_actions.extend(self._attempt_diff_actions(state))
        # `status` comes from the adapter, which maps a process exit code and
        # knows nothing about whether the work happened -- a run whose every
        # tool call was denied still reaches here as `done`. Say what it
        # actually describes; the assessed verdict belongs to the ledger and is
        # what the task pill shows.
        markdown_parts = [
            f"### {provider_label} result",
            f"Process: `{status}`",
            f"Task: {task}",
        ]
        if tools:
            markdown_parts.append(f"Tools: {', '.join(tools[:5])}")
        markdown_parts.extend(["", result or "No result text was returned."])
        markdown = "\n".join(markdown_parts)
        state["report_markdown"] = markdown
        report_lead = self._trim(result, 180) or task
        state["report_view"] = {
            "phase": "Result",
            "title": f"{provider_label} result report",
            "lead": report_lead,
            "progress": 100,
        }
        result_signals = [
            work_signal(
                label="status",
                text=f"{provider_label} returned {status}.",
                detail=provider_label,
                kind="status",
                presentation={
                    "text": presentation_message("provider.returned", provider=provider_label, status=status),
                },
            ),
            work_signal(
                label="tools",
                text=f"{len(tools)} tool event(s) observed.",
                detail=", ".join(tools[:3]) if tools else "none",
                kind="tool",
                presentation={
                    "text": presentation_message("tool.event_count", count=len(tools)),
                    **(
                        {}
                        if tools
                        else {"detail": presentation_message("status.none")}
                    ),
                },
            ),
        ]
        await bus.emit(
            Method.WALLPAPER_CANVAS,
            markdown_canvas_payload(
                phase="Result",
                title=f"{provider_label} result report",
                lead=report_lead,
                markdown=markdown,
                signals=result_signals,
                actions=source_actions,
                progress=100,
                size_preset="compact",
                metadata=self._canvas_metadata(state, artifact_type="report"),
                presentation={
                    "title": presentation_message("provider.result_report", provider=provider_label),
                    "markdown": presentation_message(
                        "provider.result_markdown",
                        provider=provider_label,
                        status=status,
                        task=task,
                        tools=", ".join(tools[:5]) if tools else "none",
                        result=result or "No result text was returned.",
                    ),
                },
            ),
        )
        if self._ledger_owns_terminal_note(state):
            # The process exiting is not the task being done: the ledger still
            # has to cross-check the tool evidence and the git delta, and may
            # need an approval first. Keep the result canvas, but let the ledger
            # publish the permission checkpoint or the terminal WorkNote. This
            # also removes the provider-result/permission race between
            # concurrent bus handlers -- and the narration race behind it, since
            # both sides used to be able to speak about the same ending.
            state["release_owned_by_observer"] = True
            return
        await self._emit_work_note(
            state,
            phase="Result",
            title=f"{provider_label} result report",
            summary=self._trim(result, 220) or task,
            signals=result_signals,
            importance="important",
            metadata_extra={"narration_keypoint": "terminal"},
        )

    @staticmethod
    def _ledger_owns_terminal_note(state: dict[str, Any]) -> bool:
        """True when the ledger will assess this run, and so should narrate it.

        Keyed on whether an assessment is coming, not on which provider ran:
        naming a provider here would confuse a transport name with "this run has a
        tracked attempt to cross-check". Runs the ledger does not track keep
        their WorkActivity narration, which is also the fallback if the
        assessment never arrives.
        """

        from config import settings as _settings

        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
        assessed = bool(
            str(work.get("work_item_id") or work.get("workItemId") or "").strip()
            and str(work.get("attempt_id") or work.get("attemptId") or "").strip()
        )
        if not assessed:
            return False
        completion = (
            metadata.get("provider_completion")
            if isinstance(metadata.get("provider_completion"), dict)
            else {}
        )
        if completion.get("classification") == "progress_only_completion":
            # This native terminal is an intermediate control-plane boundary.
            # The ledger may start one bounded successor, so WorkActivity must
            # never announce the predecessor as the task's final failure even
            # when the legacy global ownership switch is disabled.
            return True
        if isinstance(metadata.get("export_plan"), dict):
            return True
        return bool(getattr(_settings, "WORK_LEDGER_OWNS_TERMINAL_NARRATION", False))

    def _progress_signals(
        self,
        state: dict[str, Any],
        *,
        phase: str,
        text: str,
        semantic: bool = False,
        semantic_candidate: bool = False,
    ) -> list[dict[str, Any]]:
        provider = str(state.get("provider") or "provider")
        provider_label = self._provider_display_label(provider)
        tools = [str(item) for item in (state.get("tools") or []) if str(item).strip()]
        signals: list[dict[str, Any]] = [
            work_signal(
                label="provider",
                text=f"{provider_label} is in {phase.lower()} phase.",
                detail=str(state.get("run_id") or ""),
                kind="status",
                presentation={
                    "text": presentation_message(
                        "provider.phase",
                        provider=provider_label,
                        phase=phase,
                    ),
                },
            ),
        ]
        if tools:
            signals.append(
                work_signal(
                    label="tool",
                    text=f"Latest tool event: {tools[-1]}",
                    detail=f"{len(tools)} event(s)",
                    kind="tool",
                    presentation={
                        "text": presentation_message("tool.latest", tool=tools[-1]),
                        "detail": presentation_message("event.count", count=len(tools)),
                    },
                )
            )
        if text:
            signals.append(
                work_signal(
                    label="report" if semantic else "stream",
                    text=self._trim(text, 110),
                    detail=(
                        "reported direction; not verified"
                        if semantic_candidate
                        else "Host-observed semantic evidence"
                        if semantic and state.get("semantic_verified") is True
                        else "provider-reported semantic evidence; not verified"
                        if semantic
                        else "streaming"
                    ),
                    kind="report" if semantic else "status",
                    importance="important" if semantic else "normal",
                    presentation={
                        "detail": presentation_message(
                            "progress.not_terminal"
                            if semantic_candidate
                            else "progress.semantic"
                            if semantic
                            else "progress.streaming"
                        ),
                    },
                )
            )
        else:
            signals.append(
                work_signal(
                    label="task",
                    text=self._trim(str(state.get("task") or "Provider task"), 100),
                    detail="delegated",
                    kind="run",
                    presentation={
                        "text": presentation_message("task.selected"),
                    },
                )
            )
        diagnostic = (
            state.get("last_permission_diagnostic")
            if isinstance(state.get("last_permission_diagnostic"), dict)
            else {}
        )
        if diagnostic:
            tool = str(diagnostic.get("tool") or "").strip()
            capability = str(diagnostic.get("capability") or "provider action").strip()
            action = str(diagnostic.get("action") or "invoke").strip()
            signals.append(
                work_signal(
                    label="permission",
                    text=f"{tool or capability}: {action} was denied by provider policy.",
                    detail="The run may continue with a narrower alternative.",
                    kind="permission",
                    importance="important",
                    presentation={
                        "text": presentation_message(
                            "permission.denied_operation",
                            operation=tool or f"{capability}: {action}",
                        ),
                        "detail": presentation_message("permission.narrower_alternative"),
                    },
                )
            )
        return signals[:4]

    def _provider_actions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        provider = str(state.get("provider") or "provider")
        run_id = str(state.get("run_id") or "").strip()
        cwd = str(state.get("cwd") or "").strip()
        metadata = {
            "target": "provider",
            "provider": provider,
            "run_id": run_id,
            "cwd": cwd,
        }
        return [
            action_ref(
                kind="provider",
                label="Open Provider details",
                default_action="open_details",
                actions=["open_details"],
                risk="local_view",
                ref=run_id,
                metadata={**metadata, "label": "Open Provider details"},
            ),
        ]

    def _attempt_diff_actions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Offer historical diff review only for a tracked workspace Attempt.

        The Work Ledger, rather than a Provider name or a guessed task kind,
        owns whether a run has a filesystem review boundary.  A workspace-less
        Browser/research Attempt has no diff; a workspace-backed Attempt keeps
        the same review entry for Git changes and staged external exports.
        """

        request_metadata = (
            state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        )
        work = (
            request_metadata.get("work")
            if isinstance(request_metadata.get("work"), dict)
            else {}
        )
        work_item_id = str(
            work.get("work_item_id") or work.get("workItemId") or ""
        ).strip()
        attempt_id = str(work.get("attempt_id") or work.get("attemptId") or "").strip()
        workspace_mode = str(
            work.get("workspace_mode") or work.get("workspaceMode") or ""
        ).strip().lower()
        if not work_item_id or not attempt_id or not workspace_mode or workspace_mode == "none":
            return []

        provider = str(state.get("provider") or "provider")
        run_id = str(state.get("run_id") or "").strip()
        cwd = str(state.get("cwd") or work.get("workspace_path") or "").strip()
        return [
            action_ref(
                kind="provider",
                label="View diff",
                default_action="view_diff",
                actions=["view_diff"],
                risk="local_view",
                ref=run_id or attempt_id,
                metadata={
                    "target": "provider",
                    "provider": provider,
                    "run_id": run_id,
                    "cwd": cwd,
                    "work_item_id": work_item_id,
                    "attempt_id": attempt_id,
                    "label": "View diff",
                },
            )
        ]

    def _host_label(self, url: str) -> str:
        text = str(url or "").strip()
        if not text:
            return ""
        try:
            from urllib.parse import urlparse

            return urlparse(text).netloc.removeprefix("www.")
        except Exception:
            return ""

    @staticmethod
    def _provider_display_label(provider: str) -> str:
        value = str(provider or "provider").strip()
        if value.lower().replace("_", "-") in {
            "codex",
            "codex-app-server",
            "direct-codex",
        }:
            return "Codex"
        try:
            from agent_host.provider_runtime import runtime as provider_runtime

            manifest = provider_runtime.get_manifest(value)
            if manifest is not None and str(manifest.display_name or "").strip():
                return str(manifest.display_name).strip()
        except Exception:
            pass
        return value.title() if value else "Provider"

    @staticmethod
    def _latest_browser_snapshot_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        provider_branch = metadata.get("provider_branch") if isinstance(metadata.get("provider_branch"), dict) else {}
        artifacts = provider_branch.get("artifacts") if isinstance(provider_branch.get("artifacts"), list) else []
        for item in reversed(artifacts):
            if not isinstance(item, dict):
                continue
            artifact_type = str(item.get("artifact_type") or item.get("type") or "").strip().lower()
            if artifact_type == "browser.snapshot":
                return dict(item)
        return {}

    async def _emit_work_note(
        self,
        state: dict[str, Any],
        *,
        phase: str,
        title: str,
        summary: str,
        signals: list[dict[str, Any]],
        importance: str,
        observer_policy: str = "auto",
        metadata_extra: dict[str, Any] | None = None,
    ) -> None:
        policy = str(observer_policy or "auto").strip().lower()
        phase_text = str(phase or "").strip().lower()
        if policy != "silent" and phase_text == "result":
            state["release_owned_by_observer"] = True
        metadata = dict(state.get("metadata") if isinstance(state.get("metadata"), dict) else {})
        if metadata_extra:
            metadata.update(metadata_extra)
        note = work_note_payload(
            source="provider",
            provider=str(state.get("provider") or "provider"),
            run_id=str(state.get("run_id") or ""),
            session_id=self._session_id(state),
            phase=phase,
            title=title,
            summary=self._trim(summary, 240),
            signals=signals,
            importance=importance,
            observer_policy=observer_policy,
            metadata=metadata,
            speak=False,
        )
        add_work_note(note)
        await bus.emit(Method.CHAT_WORK_NOTE, note)

    @staticmethod
    def _session_id(state: dict[str, Any]) -> str:
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        return str(metadata.get("session_id") or "")

    @staticmethod
    def _trim(text: str, limit: int) -> str:
        cleaned = " ".join(str(text or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)].rstrip() + "..."

    @staticmethod
    def _trim_tail(text: str, limit: int) -> str:
        cleaned = " ".join(str(text or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return "..." + cleaned[-max(0, limit - 3) :].lstrip()

    @staticmethod
    def _format_elapsed(elapsed_s: float) -> str:
        seconds = max(0, int(elapsed_s))
        minutes, second = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minute}m {second}s"
        if minute:
            return f"{minute}m {second}s"
        return f"{second}s"

    def _source_actions(self, state: dict[str, Any], result: str) -> list[dict[str, Any]]:
        urls: list[str] = []
        self._collect_urls(str(result or ""), urls)
        raw_events = state.get("raw_tool_events") if isinstance(state.get("raw_tool_events"), list) else []
        for event in raw_events:
            if isinstance(event, dict):
                self._collect_urls_from_tool_event(event, urls)
        # Keep direct paper/PDF links first, then preserve the original order.
        deduped: list[str] = []
        seen: set[str] = set()
        for url in sorted(urls, key=self._source_priority):
            if not self._is_useful_source_url(url):
                continue
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(url)
            if len(deduped) >= 4:
                break
        return [
            action_ref(
                kind="url",
                label=self._source_label(url),
                default_action="open",
                actions=["open", "copy", "source"],
                risk="local_view",
                url=url,
                uri=url,
                metadata={"source": "provider_result"},
            )
            for url in deduped
        ]

    def _collect_urls_from_tool_event(self, event: dict[str, Any], urls: list[str]) -> None:
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        if isinstance(args, dict):
            self._collect_urls(str(args.get("url") or ""), urls)
            self._collect_urls(str(args.get("query") or ""), urls)
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        content = result.get("content") if isinstance(result.get("content"), list) else []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "")
                self._collect_urls(text, urls)
                try:
                    wrapper = json.loads(text)
                except Exception:
                    wrapper = {}
                if isinstance(wrapper, dict):
                    for key in ("url", "finalUrl", "source", "href"):
                        self._collect_urls(str(wrapper.get(key) or ""), urls)
                    self._collect_urls(str(wrapper.get("text") or ""), urls)
        self._collect_urls(str(event.get("meta") or ""), urls)

    @staticmethod
    def _collect_urls(text: str, urls: list[str]) -> None:
        if not text:
            return
        for match in re.finditer(r"https?://[^\s<>'\"`*]+", str(text), flags=re.I):
            url = match.group(0).rstrip("\\.,;:!?)]}）】》。*_")
            if url:
                urls.append(url)

    @staticmethod
    def _is_useful_source_url(url: str) -> bool:
        lowered = str(url or "").lower()
        if not lowered.startswith(("http://", "https://")):
            return False
        noisy_tokens = (
            "api.duckduckgo.com",
            "duckduckgo.com/html",
            "duckduckgo.com/?",
            "google.com/search",
            "bing.com/search",
            "rate-limit",
            "placeholder",
            "localhost",
            "127.0.0.1",
        )
        return not any(token in lowered for token in noisy_tokens)

    @staticmethod
    def _source_priority(url: str) -> tuple[int, int]:
        lowered = str(url or "").lower()
        score = 4
        if ".pdf" in lowered:
            score = 0
        elif any(token in lowered for token in ("lamport", "microsoft.com", "research.microsoft", "dblp", "wikipedia")):
            score = 1
        elif any(token in lowered for token in ("duckduckgo", "google", "bing")):
            score = 3
        return (score, len(lowered))

    @staticmethod
    def _source_label(url: str) -> str:
        parsed = urlparse(str(url or ""))
        host = (parsed.netloc or "source").removeprefix("www.")
        path = parsed.path.rstrip("/")
        name = path.rsplit("/", 1)[-1] if path else ""
        if name and len(name) <= 64:
            return name
        return host

    def _should_emit_stream_text(self, text: str) -> bool:
        cleaned = " ".join(str(text or "").split())
        if len(cleaned) < self._MIN_STREAM_TEXT_CHARS:
            return False
        return True

    async def _emit_behavior_intent(self, *, reason: str, run_id: str) -> None:
        now = time.monotonic()
        if now - self._last_behavior_intent_at < self._BEHAVIOR_THROTTLE_SECONDS:
            return
        self._last_behavior_intent_at = now
        await character_presentation.claim(
            source_kind="work",
            source_id="active-work",
            label="work",
            scenario="computer-use",
            metadata={"reason": reason, "run_id": run_id},
        )

    async def _release_behavior_intent(self, *, reason: str, run_id: str) -> None:
        await character_presentation.release(
            source_kind="work",
            source_id="active-work",
            scenario="computer-use",
            metadata={"reason": reason, "run_id": run_id},
        )

    def _schedule_observer_release_fallback(self, state: dict[str, Any], *, run_id: str, reason: str) -> None:
        if state.get("observer_release_fallback_scheduled"):
            return
        state["observer_release_fallback_scheduled"] = True
        asyncio.create_task(
            self._release_behavior_after_observer_timeout(run_id=run_id, reason=reason),
            name=f"work-release-fallback-{run_id or 'provider'}",
        )

    async def _release_behavior_after_observer_timeout(self, *, run_id: str, reason: str) -> None:
        await asyncio.sleep(self._OBSERVER_RELEASE_FALLBACK_S)
        if self._active_runs:
            return
        await self._release_presentation(run_id=run_id, reason=f"{reason}:observer_fallback")
