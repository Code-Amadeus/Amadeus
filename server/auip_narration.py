"""AUIP-specific Observer/Narrator adapter.

AUIP is an experience source, not a Work Provider.  This adapter consumes only
events already accepted by :class:`AuipRuntime`, applies an AUIP scene profile,
asks an Observer whether a fact deserves a line, asks a separate Narrator for
the role prose, and finally uses the source-neutral narration delivery boundary.

Production enables it when the configured Observer/Narrator model is available.
The source-local profile remains responsible for sparse admission before any
model or shared voice resource is used.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from llm.prompts import wrap_user_message_for_language_lock
from server.auip_contract import AuipProtocolError
from server.auip_narration_llm import (
    AUIP_STRUCTURED_PRESENTATION_PROMPT,
    AUIP_STRUCTURED_REQUIRED_PROMPT,
)
from server.auip_runtime import AuipRuntime
from server.auip_structured_presentation import (
    AuipPresentationDecision,
    build_structured_presentation_payload,
    compile_auip_decision_context,
    compile_auip_host_facts,
    compile_auip_operator_fact,
    parse_structured_presentation_decision,
    semantic_commentary_facts,
)
from server.assistant_language import (
    current_assistant_language,
    text_matches_assistant_language,
)
from server.inherited_role_prompt import inherited_main_role_prompt
from server.event_bus import bus
from server.narration_delivery import (
    NarrationRequest,
    NarrationSink,
    deliver_narration,
)
from server.protocol import Method

logger = logging.getLogger(__name__)

ObserverAction = Literal["silent", "surface", "speak"]
AuipObserver = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any] | None] | None]
AuipNarrator = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any] | None] | None]
AuipPresenter = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any] | None] | None]
RecentChat = Callable[[str], list[dict[str, str]]]
DisplayLanguage = Callable[[], str]
RolePrompt = Callable[[], str]


AUIP_NARRATOR_SYSTEM_PROMPT = """You are continuing the main assistant's role inside a short-lived AUIP experience branch.

The inherited main-chat role prompt remains authoritative for identity,
language, tone, and character behavior. You are the same assistant, not a
separate game or tool persona.

The Host-selected fact_brief is the complete and authoritative scene fact
for this call. Phrase it in character without
reinterpreting actors, sides, actions, or outcomes. Do not invent application
state or hidden reasoning. Avoid repeating a recently delivered line. A
comment is optional; never emit DELEGATE tags or operational instructions.
Prefer one natural reaction to a metric-by-metric status report. Mention an
exact number only when the Host fact brief makes that number itself the
meaningful outcome.
`app.interactionSummary`, when present, is background domain knowledge with
examples, not evidence that any example occurred. Use it only to understand
the fact brief and choose natural terminology or tone.
When `delivery_source` is `auip_operator_outcome`, the failed or rejected
request belongs to the assistant Participant lane. Acknowledge that failure in
the first person; never blame, scold, hurry, or instruct the user for it.

You deliberately do not receive the raw main-chat transcript. The inherited
role prompt owns identity and voice; fact_brief owns this scene's truth. This
prevents an older assistant promise or report from becoming a new scene fact.

Return JSON only:
{"display_text":"one short in-character line","emotion":"one short label"}
"""


@dataclass(frozen=True, slots=True)
class AuipNarrationProfile:
    """Host-owned admission policy for one class of experiences."""

    profile_id: str = "game"
    normal_beat_stride: int = 3
    observe_ambient: bool = False
    max_spoken_chars: int = 96
    # Sparse does not mean permanently silent. After this many accepted
    # assistant actions without one delivered AUIP line, source policy makes
    # the next verified action commentary due. The Narrator still owns prose.
    max_silent_self_actions: int = 2
    # One accepted action may publish several semantic consequences at the
    # same revision. Give the Host a tiny window to present the most important
    # consequence once instead of racing one model call per event.
    action_event_coalesce_s: float = 0.05

    def should_observe(
        self,
        event: dict[str, Any],
        *,
        normal_beat_index: int,
        verified_self_action: dict[str, Any] | None = None,
    ) -> bool:
        if event.get("beat") is not True:
            return False
        if event.get("terminal") is True:
            return True
        importance = str(event.get("importance") or "normal").strip().lower()
        if importance in {"important", "blocking"}:
            return True
        if _follows_verified_self_action(event, verified_self_action):
            # Admission is not a command to speak.  It gives the Observer a
            # chance to comment on a real assistant action while retaining its
            # right to stay silent.  Requiring the matching accepted receipt
            # prevents an untrusted app from minting role facts by merely
            # labelling one of its own events actor=kurisu.
            return True
        if importance == "ambient" and not self.observe_ambient:
            return False
        stride = max(1, int(self.normal_beat_stride))
        return normal_beat_index % stride == 0


GAME_NARRATION_PROFILE = AuipNarrationProfile()


class AuipNarrationAdapter:
    """Keep AUIP interpretation above the shared delivery boundary."""

    def __init__(
        self,
        *,
        runtime: AuipRuntime,
        observer: AuipObserver,
        narrator: AuipNarrator,
        sink: NarrationSink,
        presenter: AuipPresenter | None = None,
        presentation_mode: str = "split",
        profile: AuipNarrationProfile = GAME_NARRATION_PROFILE,
        recent_chat: RecentChat | None = None,
        display_language: DisplayLanguage | None = None,
        role_prompt: RolePrompt | None = None,
    ) -> None:
        self.runtime = runtime
        self.observer = observer
        self.narrator = narrator
        self.presenter = presenter
        self.presentation_mode = str(presentation_mode or "split").strip().lower()
        if self.presentation_mode not in {"split", "structured"}:
            raise ValueError(f"unsupported AUIP presentation mode: {presentation_mode}")
        if self.presentation_mode == "structured" and self.presenter is None:
            raise ValueError("structured AUIP presentation requires a presenter")
        self.sink = sink
        self.profile = profile
        self.recent_chat = recent_chat or (lambda _session_id: [])
        self.display_language = display_language or current_assistant_language
        self.role_prompt = role_prompt or (lambda: inherited_main_role_prompt("base"))
        self._normal_beat_counts: dict[str, int] = {}
        self._silent_self_actions: dict[str, int] = {}
        self._terminal_revision_by_session: dict[str, int] = {}
        self._direct_controller_leases: set[tuple[str, str]] = set()
        self._inflight: set[str] = set()
        self._completed: set[str] = set()
        self._completed_order: deque[str] = deque(maxlen=256)
        self._serial_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[dict[str, Any] | None]] = set()
        self._background_task_sessions: dict[asyncio.Task[Any], str] = {}
        self._controller_generation_by_session: dict[str, int] = {}
        self._preferred_action_events: dict[
            tuple[str, str], tuple[int, int, str]
        ] = {}
        self._event_enqueue_order = 0

    async def enqueue_update(
        self,
        method: str,
        payload: dict[str, Any],
    ) -> None:
        """Detach narration from the application event acknowledgement path.

        AUIP state and action receipts are authoritative before commentary.  A
        slow Observer, Narrator, or voice queue must therefore never delay the
        app's publish response.  ``handle_update`` remains serial so accepted
        facts are narrated in source order.
        """

        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        app_session_id = str(payload.get("app_session_id") or "").strip()
        controller = (
            payload.get("controller")
            if isinstance(payload.get("controller"), dict)
            else {}
        )
        lease = (
            controller.get("lease")
            if isinstance(controller.get("lease"), dict)
            else {}
        )
        generation = int(lease.get("generation") or 0)
        previous_generation = self._controller_generation_by_session.get(
            app_session_id,
            0,
        )
        if app_session_id and generation > previous_generation:
            self._controller_generation_by_session[app_session_id] = generation
            self.cancel_pending(app_session_id=app_session_id)
        if event.get("terminal") is True:
            # A terminal fact supersedes commentary that has not reached the
            # user.  Otherwise a fast game can finish while several old board
            # states are still being narrated ahead of its actual outcome.
            self.cancel_pending(app_session_id=app_session_id)
        action_key: tuple[str, str] | None = None
        event_id = str(event.get("event_id") or "").strip()
        caused_by_action_id = str(event.get("caused_by_action_id") or "").strip()
        if app_session_id and event_id and caused_by_action_id:
            action_key = (app_session_id, caused_by_action_id)
            self._event_enqueue_order += 1
            preference = (
                _action_event_presentation_priority(event),
                self._event_enqueue_order,
                event_id,
            )
            current = self._preferred_action_events.get(action_key)
            if current is None or preference[:2] > current[:2]:
                self._preferred_action_events[action_key] = preference
        task = asyncio.create_task(
            self._handle_enqueued_update(
                method,
                dict(payload),
                action_key=action_key,
                event_id=event_id,
            )
        )
        self._background_tasks.add(task)
        self._background_task_sessions[task] = app_session_id

        def forget(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            self._background_task_sessions.pop(done, None)

        task.add_done_callback(forget)

    async def _handle_enqueued_update(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        action_key: tuple[str, str] | None,
        event_id: str,
    ) -> dict[str, Any] | None:
        """Coalesce one accepted action's event burst before presentation."""

        try:
            if action_key is not None:
                await asyncio.sleep(max(0.0, float(self.profile.action_event_coalesce_s)))
                preferred = self._preferred_action_events.get(action_key)
                if preferred is not None and preferred[2] != event_id:
                    request_id = f"auip-narration-{action_key[0]}-{event_id}"
                    self._remember(request_id)
                    return {
                        "status": "superseded_by_action_consequence",
                        "request_id": request_id,
                    }
            return await self.handle_update(method, payload)
        finally:
            if action_key is not None:
                preferred = self._preferred_action_events.get(action_key)
                if preferred is not None and preferred[2] == event_id:
                    self._preferred_action_events.pop(action_key, None)

    def cancel_pending(self, *, app_session_id: str = "") -> int:
        """Cancel source-local interpretation that has not reached delivery."""

        target = str(app_session_id or "").strip()
        pending = tuple(
            task
            for task in self._background_tasks
            if not task.done()
            and (
                not target
                or self._background_task_sessions.get(task) == target
            )
        )
        for task in pending:
            task.cancel()
        return len(pending)

    async def close(self) -> None:
        """Cancel narration work that has not finished during host shutdown."""

        tasks = tuple(self._background_tasks)
        self.cancel_pending()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._preferred_action_events.clear()

    async def wait_for_idle(self) -> None:
        """Wait until all currently enqueued source facts finish delivery."""

        while self._background_tasks:
            tasks = tuple(self._background_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            # Task done callbacks run on the event loop's ready queue. A tight
            # loop that immediately gathers the same completed tasks can starve
            # those callbacks indefinitely. Retire this completed snapshot
            # here; tasks enqueued while waiting remain for the next iteration.
            for task in tasks:
                if task.done():
                    self._background_tasks.discard(task)
                    self._background_task_sessions.pop(task, None)

    async def handle_update(
        self,
        _method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Observe one AUIP update; state-only updates intentionally stay silent."""

        async with self._serial_lock:
            return await self._handle_update(_method, payload)

    async def _handle_update(
        self,
        _method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Process one verified update after source-order serialization."""

        event_hint = payload.get("event") if isinstance(payload.get("event"), dict) else None
        operator_outcome = (
            payload.get("operator_outcome")
            if isinstance(payload.get("operator_outcome"), dict)
            else None
        )
        if event_hint is None:
            return (
                await self._handle_operator_outcome(payload, operator_outcome)
                if operator_outcome is not None
                else None
            )
        app_session_id = str(payload.get("app_session_id") or "").strip()
        event_id = str(event_hint.get("event_id") or "").strip()
        if not app_session_id or not event_id:
            return None
        request_id = f"auip-narration-{app_session_id}-{event_id}"
        if request_id in self._inflight or request_id in self._completed:
            return {"status": "duplicate", "request_id": request_id}

        try:
            observation = self.runtime.narration_observation(
                app_session_id=app_session_id,
                event_id=event_id,
            )
        except AuipProtocolError:
            logger.warning(
                "AUIP narration ignored unverified event app_session_id=%s event_id=%s",
                app_session_id,
                event_id,
            )
            return {"status": "unverified", "request_id": request_id}

        event = observation["event"]
        controller_key = _controller_effect_key(event)
        if controller_key is not None and not self._controller_event_is_current(
            app_session_id,
            event,
        ):
            self._remember(request_id)
            return {
                "status": "superseded_by_controller_policy",
                "request_id": request_id,
            }
        direct_controller_effect = bool(
            controller_key is not None
            and controller_key not in self._direct_controller_leases
        )
        verified_self_action = (
            observation.get("latest_verified_self_action")
            if isinstance(observation.get("latest_verified_self_action"), dict)
            else None
        )
        follows_self_action = _follows_verified_self_action(
            event,
            verified_self_action,
        )
        recent_deliveries = [
            item
            for item in observation.get("recent_delivered_narrations") or []
            if isinstance(item, dict)
        ]
        latest_delivery = recent_deliveries[-1] if recent_deliveries else {}
        foreground_action_id = str(
            (verified_self_action or {}).get("action_id") or ""
        ).strip()
        foreground_line_already_delivered = bool(
            foreground_action_id
            and str(latest_delivery.get("event_id") or "").strip()
            == foreground_action_id
            and str(latest_delivery.get("text") or "").strip()
        )
        if (
            follows_self_action
            and event.get("controller_effect") is not True
            and str((verified_self_action or {}).get("proposal_id") or "").startswith(
                "b2f:"
            )
            and (
                event.get("terminal") is not True
                or foreground_line_already_delivered
            )
        ):
            # B2 already generated one role line in the same decision that
            # selected this exact action. The line is released only after the
            # accepted receipt. A consequence narrator for the same action
            # would create a second mouth and race the foreground delivery.
            self._remember(request_id)
            return {
                "status": (
                    "b2_foreground_terminal_owned"
                    if event.get("terminal") is True
                    else "b2_foreground_owned"
                ),
                "request_id": request_id,
            }
        if follows_self_action:
            self._silent_self_actions[app_session_id] = (
                self._silent_self_actions.get(app_session_id, 0) + 1
            )
        silent_self_actions = self._silent_self_actions.get(app_session_id, 0)
        commentary_due = bool(
            follows_self_action
            and int(self.profile.max_silent_self_actions) > 0
            and silent_self_actions >= int(self.profile.max_silent_self_actions)
        )
        event_revision = int(event.get("revision") or 0)
        if event.get("terminal") is True:
            self._terminal_revision_by_session[app_session_id] = max(
                event_revision,
                self._terminal_revision_by_session.get(app_session_id, 0),
            )
        elif event_revision <= self._terminal_revision_by_session.get(app_session_id, 0):
            self._remember(request_id)
            return {"status": "superseded_by_terminal", "request_id": request_id}
        if (
            event.get("terminal") is not True
            and not direct_controller_effect
            and int(observation.get("revision") or 0) != int(event_revision)
        ):
            self._remember(request_id)
            return {"status": "stale_event", "request_id": request_id}
        importance = str(event.get("importance") or "normal").strip().lower()
        if event.get("beat") is True and importance not in {"important", "blocking"} and event.get("terminal") is not True:
            count = self._normal_beat_counts.get(app_session_id, 0) + 1
            self._normal_beat_counts[app_session_id] = count
        else:
            count = self._normal_beat_counts.get(app_session_id, 0)
        if not self.profile.should_observe(
            event,
            normal_beat_index=count,
            verified_self_action=verified_self_action,
        ):
            self._remember(request_id)
            return {"status": "profile_filtered", "request_id": request_id}
        self._inflight.add(request_id)
        try:
            checkpoint = {
                "conversation_id": str(observation.get("conversation_id") or ""),
                "recent_messages": _bounded_chat(
                    self.recent_chat(str(observation.get("conversation_id") or ""))
                ),
                "recent_delivered_narrations": _bounded_delivered(
                    observation.get("recent_delivered_narrations")
                ),
            }
            observer_input = {
                **observation,
                "source": "auip_host_observation",
                "profile_id": self.profile.profile_id,
                "conversation_checkpoint": checkpoint,
                "display_language": _display_language(self.display_language()),
                "commentary_due": commentary_due,
                "silent_self_action_count": silent_self_actions,
            }
            terminal_due = event.get("terminal") is True
            important_due = importance in {"important", "blocking"}
            if self.presentation_mode == "structured":
                if direct_controller_effect:
                    self._direct_controller_leases.add(controller_key)
                host_reason_code = (
                    "terminal"
                    if terminal_due
                    else "consequence"
                    if direct_controller_effect or important_due
                    else "commentary_due"
                    if commentary_due
                    else ""
                )
                structured_facts = compile_auip_host_facts(observation)
                structured_decision_context = compile_auip_decision_context(
                    observation
                )
                semantic_commentary_due = bool(
                    commentary_due and structured_decision_context
                )
                presentation_facts = (
                    semantic_commentary_facts(structured_facts)
                    if semantic_commentary_due
                    and not terminal_due
                    and not direct_controller_effect
                    and not important_due
                    else structured_facts
                )
                structured_decision = await self._decide_structured_presentation(
                    request_id=request_id,
                    app=observation["app"],
                    facts=presentation_facts,
                    checkpoint=checkpoint,
                    display_language=observer_input["display_language"],
                    # Application-authored important results and true terminal
                    # events must be delivered. Commentary debt becomes
                    # mandatory only when the accepted action carries a
                    # receipt-bound semantic role reason; debt alone must not
                    # force a low-information coordinate/status line.
                    presentation_required=bool(
                        terminal_due
                        or direct_controller_effect
                        or important_due
                        or semantic_commentary_due
                    ),
                    host_reason_code=host_reason_code,
                    decision_context=structured_decision_context,
                )
                if not structured_decision.valid:
                    fallback = (
                        _terminal_fallback_line(
                            observation,
                            display_language=observer_input["display_language"],
                        )
                        if terminal_due
                        else _controller_effect_fallback_line(
                            display_language=observer_input["display_language"]
                        )
                        if direct_controller_effect
                        else _important_event_fallback_line(
                            display_language=observer_input["display_language"]
                        )
                        if important_due
                        else None
                    )
                    if fallback is None:
                        self._remember(request_id)
                        return {
                            "status": "invalid_presentation",
                            "request_id": request_id,
                            "reason": structured_decision.error,
                        }
                    structured_decision = AuipPresentationDecision(
                        action="speak",
                        selected_fact_ids=tuple(
                            str(item.get("fact_id") or "")
                            for item in structured_facts
                            if str(item.get("fact_id") or "")
                        ),
                        display_text=str(fallback.get("display_text") or ""),
                        emotion=str(fallback.get("emotion") or "thinking"),
                        reason_code="terminal",
                    )
                if structured_decision.action != "speak":
                    self._remember(request_id)
                    return {
                        "status": structured_decision.action,
                        "request_id": request_id,
                        "selected_fact_ids": list(
                            structured_decision.selected_fact_ids
                        ),
                        "reason_code": structured_decision.reason_code,
                    }
            else:
                if terminal_due:
                    # Terminal admission is deterministic and needs no Observer
                    # model call: a verified outcome always merits one report.
                    # The Host supplies facts only; the Narrator still owns voice.
                    decision = {
                        "action": "speak",
                        "fact_brief": _terminal_fact(observation),
                    }
                elif direct_controller_effect:
                    # The Host already correlated this declared Controller effect
                    # with an active lease. Its first important effect in one
                    # policy generation is a fact, not another optional semantic
                    # decision, so it can go straight to the role Narrator.
                    self._direct_controller_leases.add(controller_key)
                    decision = {
                        "action": "speak",
                        "fact_brief": _controller_effect_fact(observation),
                    }
                else:
                    observer_result = await _call(self.observer, observer_input)
                    decision = _observer_decision(observer_result)
                if commentary_due and decision["action"] != "speak":
                    decision = {
                        "action": "speak",
                        "fact_brief": (
                            decision["fact_brief"]
                            or _verified_self_action_fact(
                                observation,
                                verified_self_action,
                            )
                        ),
                    }
                if decision["action"] != "speak":
                    self._remember(request_id)
                    return {
                        "status": decision["action"],
                        "request_id": request_id,
                        "fact_brief": decision["fact_brief"],
                    }

            if event.get("terminal") is not True and not direct_controller_effect:
                current = self.runtime.narration_observation(
                    app_session_id=app_session_id,
                    event_id=event_id,
                )
                if int(current.get("revision") or 0) != int(event.get("revision") or 0):
                    self._remember(request_id)
                    return {"status": "stale_event", "request_id": request_id}

            if self.presentation_mode == "structured":
                delivered = await self._deliver_generated_line(
                    request_id=request_id,
                    app_session_id=app_session_id,
                    conversation_id=str(observation.get("conversation_id") or ""),
                    generated={
                        "display_text": structured_decision.display_text,
                        "emotion": structured_decision.emotion,
                    },
                    display_language=observer_input["display_language"],
                    # Delivery source names the product lane, not the current
                    # internal call topology. Keep it stable for TTS/history
                    # consumers and semantic-journey evidence.
                    source="auip_narrator",
                    terminal=event.get("terminal") is True,
                    event_id=event_id,
                )
                delivered.update(
                    {
                        "selected_fact_ids": list(
                            structured_decision.selected_fact_ids
                        ),
                        "reason_code": structured_decision.reason_code,
                    }
                )
            else:
                delivered = await self._narrate_fact(
                    request_id=request_id,
                    app_session_id=app_session_id,
                    conversation_id=str(observation.get("conversation_id") or ""),
                    app=observation["app"],
                    fact_brief=decision["fact_brief"],
                    recent_delivered=checkpoint["recent_delivered_narrations"],
                    display_language=observer_input["display_language"],
                    source="auip_narrator",
                    terminal=event.get("terminal") is True,
                    event_id=event_id,
                    fallback=(
                        _terminal_fallback_line(
                            observation,
                            display_language=observer_input["display_language"],
                        )
                        if terminal_due
                        else None
                    ),
                )
            self._remember(request_id)
            return delivered
        except Exception:
            logger.exception(
                "AUIP narration adapter failed app_session_id=%s event_id=%s",
                app_session_id,
                event_id,
            )
            self._remember(request_id)
            return {"status": "error", "request_id": request_id}
        finally:
            self._inflight.discard(request_id)

    async def _decide_structured_presentation(
        self,
        *,
        request_id: str,
        app: dict[str, Any],
        facts: list[dict[str, Any]],
        checkpoint: dict[str, Any],
        display_language: str,
        presentation_required: bool,
        host_reason_code: str,
        decision_context: dict[str, str] | None = None,
    ) -> AuipPresentationDecision:
        payload = build_structured_presentation_payload(
            facts=facts,
            app=app,
            recent_messages=checkpoint.get("recent_messages") or [],
            recent_delivered_narrations=(
                checkpoint.get("recent_delivered_narrations") or []
            ),
            profile_id=self.profile.profile_id,
            display_language=display_language,
            presentation_required=presentation_required,
            host_reason_code=host_reason_code,
            decision_context=decision_context,
            user_topic_wrapper=wrap_user_message_for_language_lock,
        )
        presenter_input = {
            **payload,
            "source": "auip_structured_presentation_request",
            "request_id": request_id,
            "system_prompt": _structured_presenter_system_prompt(
                self.role_prompt(),
                max_spoken_chars=self.profile.max_spoken_chars,
                presentation_required=presentation_required,
                display_language=display_language,
            ),
        }
        result = await _call(self.presenter, presenter_input)  # type: ignore[arg-type]
        return parse_structured_presentation_decision(
            result,
            facts=facts,
            presentation_required=presentation_required,
            max_spoken_chars=self.profile.max_spoken_chars,
        )

    async def _handle_operator_outcome(
        self,
        payload: dict[str, Any],
        outcome: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Narrate a Host-owned inability without forging an app event."""

        if str(outcome.get("status") or "").strip().lower() != "blocked":
            return None
        app_session_id = str(payload.get("app_session_id") or "").strip()
        outcome_id = str(
            outcome.get("outcome_id") or outcome.get("proposal_id") or ""
        ).strip()
        if not app_session_id:
            return None
        snapshot = self.runtime.get(app_session_id)
        reason = " ".join(str(outcome.get("reason") or "").split())[:600]
        if (
            str(snapshot.get("operator_status") or "") != "error"
            or not str(snapshot.get("operator_error") or "").strip()
            or not reason
        ):
            return None
        request_key = outcome_id or (
            f"generation-{int(snapshot.get('decision_generation') or 0)}"
        )
        request_id = f"auip-operator-blocked-{app_session_id}-{request_key}"
        if request_id in self._inflight or request_id in self._completed:
            return {"status": "duplicate", "request_id": request_id}
        self._inflight.add(request_id)
        try:
            display_language = _display_language(self.display_language())
            latest = snapshot.get("latest_delivered_narration")
            recent = [latest] if isinstance(latest, dict) else []
            app = snapshot.get("app") if isinstance(snapshot.get("app"), dict) else {}
            conversation_id = str(snapshot.get("conversation_id") or "")
            fallback = _operator_blocked_fallback_line(
                reason,
                display_language=display_language,
            )
            if self.presentation_mode == "structured":
                facts = compile_auip_operator_fact(
                    app_session_id=app_session_id,
                    outcome_id=request_key,
                    revision=int(snapshot.get("revision") or 0),
                    reason=reason,
                )
                decision = await self._decide_structured_presentation(
                    request_id=request_id,
                    app=app,
                    facts=facts,
                    checkpoint={
                        "recent_messages": _bounded_chat(
                            self.recent_chat(conversation_id)
                        ),
                        "recent_delivered_narrations": _bounded_delivered(recent),
                    },
                    display_language=display_language,
                    presentation_required=True,
                    host_reason_code="consequence",
                )
                generated = (
                    {
                        "display_text": decision.display_text,
                        "emotion": decision.emotion,
                    }
                    if decision.valid and decision.action == "speak"
                    else fallback
                )
                result = await self._deliver_generated_line(
                    request_id=request_id,
                    app_session_id=app_session_id,
                    conversation_id=conversation_id,
                    generated=generated,
                    display_language=display_language,
                    source="auip_operator_outcome",
                    terminal=False,
                )
                result.update(
                    {
                        "selected_fact_ids": (
                            list(decision.selected_fact_ids)
                            if decision.valid
                            else [str(item.get("fact_id") or "") for item in facts]
                        ),
                        "reason_code": (
                            decision.reason_code if decision.valid else "consequence"
                        ),
                    }
                )
            else:
                fact_brief = (
                    "Kurisu's own assigned participant request was not confirmed as "
                    "performed; this is not a failure by the user. "
                    f"Reason: {reason} No accepted execution receipt establishes that "
                    "the action happened. The line must not blame or direct the user."
                )[:900]
                result = await self._narrate_fact(
                    request_id=request_id,
                    app_session_id=app_session_id,
                    conversation_id=conversation_id,
                    app=app,
                    fact_brief=fact_brief,
                    recent_delivered=_bounded_delivered(recent),
                    display_language=display_language,
                    source="auip_operator_outcome",
                    terminal=False,
                    fallback=fallback,
                )
            self._remember(request_id)
            return result
        except Exception:
            logger.exception(
                "AUIP operator outcome narration failed app_session_id=%s",
                app_session_id,
            )
            self._remember(request_id)
            return {"status": "error", "request_id": request_id}
        finally:
            self._inflight.discard(request_id)

    async def _narrate_fact(
        self,
        *,
        request_id: str,
        app_session_id: str,
        conversation_id: str,
        app: dict[str, Any],
        fact_brief: str,
        recent_delivered: list[dict[str, Any]],
        display_language: str,
        source: str,
        terminal: bool,
        event_id: str = "",
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        narrator_input = {
            "source": "auip_narrator_request",
            "request_id": request_id,
            "delivery_source": source,
            "terminal": bool(terminal),
            "profile_id": self.profile.profile_id,
            "display_language": display_language,
            "system_prompt": _narrator_system_prompt(
                self.role_prompt(),
                max_spoken_chars=self.profile.max_spoken_chars,
            ),
            "recent_delivered_narrations": recent_delivered,
            "fact_brief": fact_brief,
            "app": app,
        }
        narrator_result = await _call(self.narrator, narrator_input)
        return await self._deliver_generated_line(
            request_id=request_id,
            app_session_id=app_session_id,
            conversation_id=conversation_id,
            generated=narrator_result,
            display_language=display_language,
            source=source,
            terminal=terminal,
            event_id=event_id,
            fallback=fallback,
        )

    async def _deliver_generated_line(
        self,
        *,
        request_id: str,
        app_session_id: str,
        conversation_id: str,
        generated: Any,
        display_language: str,
        source: str,
        terminal: bool,
        event_id: str = "",
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if event_id:
            try:
                current_observation = self.runtime.narration_observation(
                    app_session_id=app_session_id,
                    event_id=event_id,
                )
            except AuipProtocolError:
                return {"status": "unverified", "request_id": request_id}
            current_event = (
                current_observation.get("event")
                if isinstance(current_observation.get("event"), dict)
                else {}
            )
            if (
                _controller_effect_key(current_event) is not None
                and not self._controller_event_is_current(
                    app_session_id,
                    current_event,
                )
            ):
                return {
                    "status": "superseded_by_controller_policy",
                    "request_id": request_id,
                }
        line = _narrator_line(
            generated,
            max_chars=self.profile.max_spoken_chars,
        )
        if fallback is not None and (
            line["too_long"]
            or not line["display_text"]
            or not text_matches_assistant_language(
                line["display_text"], display_language
            )
        ):
            line = fallback
        if line["too_long"]:
            return {"status": "narration_too_long", "request_id": request_id}
        if not line["display_text"]:
            return {"status": "empty_narration", "request_id": request_id}
        if not text_matches_assistant_language(
            line["display_text"], display_language
        ):
            return {"status": "language_mismatch", "request_id": request_id}
        tts_payload = {
            "display_text": line["display_text"],
            "display_language": display_language,
            "emotion": line["emotion"],
            "duration_ms": 5600,
            "line_id": request_id,
            "turn_id": request_id,
            "complete_turn": True,
            "source": source,
            "terminal": terminal,
            "app_session_id": app_session_id,
            **({"event_id": event_id} if event_id else {}),
        }
        if _is_japanese(display_language):
            tts_payload["voice_text_ja"] = line["display_text"]
        receipt = await deliver_narration(
            NarrationRequest(
                request_id=request_id,
                source_kind="auip",
                source_id=app_session_id,
                session_id=conversation_id,
                payload=tts_payload,
            ),
            self.sink,
        )
        if receipt.accepted:
            self._silent_self_actions[app_session_id] = 0
            retained = self.runtime.record_delivered_narration(
                app_session_id=app_session_id,
                text=line["display_text"],
                terminal=terminal,
                event_id=event_id,
            )
            await bus.emit(Method.AUIP_UPDATED, retained)
        logger.info(
            "AUIP narration delivery app_session=%s source=%s status=%s retained=%s",
            app_session_id,
            source,
            receipt.status,
            receipt.accepted,
        )
        return {
            "status": receipt.status,
            "request_id": request_id,
            "receipt": receipt.to_dict(),
            "retained": receipt.accepted,
        }

    def _controller_event_is_current(
        self,
        app_session_id: str,
        event: dict[str, Any],
    ) -> bool:
        """Keep Controller commentary bound to the currently active lease."""

        event_lease = (
            event.get("controller_lease")
            if isinstance(event.get("controller_lease"), dict)
            else {}
        )
        snapshot = self.runtime.get(app_session_id)
        controller = (
            snapshot.get("controller")
            if isinstance(snapshot.get("controller"), dict)
            else {}
        )
        current_lease = (
            controller.get("lease")
            if isinstance(controller.get("lease"), dict)
            else {}
        )
        return bool(
            controller.get("status") == "active"
            and event_lease
            and current_lease
            and all(
                event_lease.get(key) == current_lease.get(key)
                for key in ("lease_id", "generation", "policy_revision")
            )
        )

    def _remember(self, request_id: str) -> None:
        if request_id in self._completed:
            return
        if len(self._completed_order) == self._completed_order.maxlen:
            expired = self._completed_order.popleft()
            self._completed.discard(expired)
        self._completed_order.append(request_id)
        self._completed.add(request_id)


async def _call(callback: Callable[[dict[str, Any]], Any], payload: dict[str, Any]) -> Any:
    result = callback(payload)
    if inspect.isawaitable(result):
        return await result
    return result


def _observer_decision(value: Any) -> dict[str, str]:
    data = value if isinstance(value, dict) else {}
    action = str(data.get("action") or "silent").strip().lower()
    if action not in {"silent", "surface", "speak"}:
        action = "silent"
    return {
        "action": action,
        "fact_brief": " ".join(str(data.get("fact_brief") or "").split())[:480],
    }


def _narrator_line(value: Any, *, max_chars: int) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    display_text = " ".join(str(data.get("display_text") or "").split())[:480]
    clean_limit = max(24, int(max_chars))
    return {
        "display_text": display_text,
        "emotion": str(data.get("emotion") or "thinking").strip()[:40] or "thinking",
        "too_long": len(display_text) > clean_limit,
    }


def _follows_verified_self_action(
    event: dict[str, Any],
    receipt: dict[str, Any] | None,
) -> bool:
    """Return true only for an event backed by the latest accepted self action."""

    if not isinstance(receipt, dict) or receipt.get("accepted") is not True:
        return False
    caused_by_action_id = str(event.get("caused_by_action_id") or "").strip()
    verified_action_id = str(receipt.get("action_id") or "").strip()
    if caused_by_action_id and verified_action_id:
        return caused_by_action_id == verified_action_id
    if str(event.get("actor") or "").strip().lower() != "kurisu":
        return False
    try:
        event_revision = int(event.get("revision"))
        resulting_revision = int(receipt.get("resulting_revision"))
    except (TypeError, ValueError):
        return False
    return event_revision == resulting_revision


def _action_event_presentation_priority(event: dict[str, Any]) -> int:
    """Rank consequences of one accepted action for one presentation slot."""

    if event.get("terminal") is True:
        return 3
    importance = str(event.get("importance") or "normal").strip().lower()
    if importance in {"blocking", "important"}:
        return 2
    return 1


def _verified_self_action_fact(
    observation: dict[str, Any],
    receipt: dict[str, Any] | None,
) -> str:
    """Build one bounded fact when sparse commentary has accrued a debt."""

    action = receipt if isinstance(receipt, dict) else {}
    event = observation.get("event") if isinstance(observation.get("event"), dict) else {}
    payload = {
        "action": str(action.get("type") or "application action"),
        "payload": action.get("payload") if isinstance(action.get("payload"), dict) else {},
        "effects": action.get("effects") if isinstance(action.get("effects"), dict) else {},
        "resulting_revision": action.get("resulting_revision"),
        "following_event": str(event.get("type") or ""),
    }
    return (
        "The application accepted this assistant action and reported the resulting state: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:360]
    )


def _terminal_fact(observation: dict[str, Any]) -> str:
    """Build a bounded Host-fact brief for a mandatory terminal report."""

    event = observation.get("event") if isinstance(observation.get("event"), dict) else {}
    state = observation.get("state") if isinstance(observation.get("state"), dict) else {}
    payload = {
        "event": str(event.get("type") or "application.completed"),
        "actor": str(event.get("actor") or "app"),
        "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
        "revision": event.get("revision"),
        "state": state,
    }
    return (
        "The application reported this verified terminal outcome: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:400]
    )


def _controller_effect_key(event: dict[str, Any]) -> tuple[str, str] | None:
    """Identify one Host-correlated important effect per Controller lease."""

    if event.get("controller_effect") is not True:
        return None
    if str(event.get("actor") or "").strip().lower() != "app":
        return None
    if str(event.get("importance") or "").strip().lower() not in {
        "important",
        "blocking",
    }:
        return None
    lease = (
        event.get("controller_lease")
        if isinstance(event.get("controller_lease"), dict)
        else {}
    )
    lease_id = str(lease.get("lease_id") or "").strip()
    policy_revision = str(lease.get("policy_revision") or "").strip()
    if not lease_id or not policy_revision:
        return None
    return lease_id, policy_revision


def _controller_effect_fact(observation: dict[str, Any]) -> str:
    """Build a bounded fact for the post-ledger Controller fast lane."""

    event = observation.get("event") if isinstance(observation.get("event"), dict) else {}
    lease = (
        event.get("controller_lease")
        if isinstance(event.get("controller_lease"), dict)
        else {}
    )
    payload = {
        "event": str(event.get("type") or "application.controller_effect"),
        "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
        "revision": event.get("revision"),
        "controller": {
            "generation": lease.get("generation"),
            "policy_revision": lease.get("policy_revision"),
        },
    }
    return (
        "The application reported this verified effect from the active local "
        "Controller policy: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:400]
    )


def _terminal_fallback_line(
    observation: dict[str, Any],
    *,
    display_language: str,
) -> dict[str, Any]:
    """Return a truthful local line when terminal role generation is unavailable."""

    app = observation.get("app") if isinstance(observation.get("app"), dict) else {}
    title = " ".join(str(app.get("title") or "").split())[:48]
    language = _display_language(display_language)
    if language == "japanese":
        text = f"{title}は終了したわ。結果は画面で確認できる。" if title else "終了したわ。結果は画面で確認できる。"
    elif language == "simplified_chinese":
        text = f"{title}已结束，结果可以在画面中确认。" if title else "应用已结束，结果可以在画面中确认。"
    else:
        text = f"{title} is finished. The result is visible on screen." if title else "The application is finished. The result is visible on screen."
    return {
        "display_text": text,
        "emotion": "thinking",
        "too_long": False,
    }


def _operator_blocked_fallback_line(
    _reason: str,
    *,
    display_language: str,
) -> dict[str, Any]:
    """Keep an explicit failed step visible when role generation is unavailable."""

    language = _display_language(display_language)
    if language == "japanese":
        text = "私の操作は確認されなかったわ。今回は完了したとは言えない。"
    elif language == "simplified_chinese":
        text = "我的这次操作没有得到确认，所以目前不能说它已经完成。"
    else:
        text = "My action was not confirmed, so I cannot say it completed."
    return {
        "display_text": text,
        "emotion": "thinking",
        "too_long": False,
    }


def _controller_effect_fallback_line(*, display_language: str) -> dict[str, Any]:
    """Keep the first verified Controller effect visible without guessing detail."""

    language = _display_language(display_language)
    if language == "japanese":
        text = "制御は実際に動いているわ。詳しい状況は画面で確認できる。"
    elif language == "simplified_chinese":
        text = "控制器已经实际运行，具体情况可以在画面中确认。"
    else:
        text = "The controller is running; the details are visible in the application."
    return {"display_text": text, "emotion": "thinking", "too_long": False}


def _important_event_fallback_line(*, display_language: str) -> dict[str, Any]:
    """Keep an app-declared important result visible when role rendering fails."""

    language = _display_language(display_language)
    if language == "japanese":
        text = "大きな変化が確定したわ。結果は画面で確認して。"
    elif language == "simplified_chinese":
        text = "一个重要变化已经确认，结果可以在画面中查看。"
    else:
        text = "An important change was confirmed; the result is visible on screen."
    return {"display_text": text, "emotion": "thinking", "too_long": False}


def _bounded_chat(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    result: list[dict[str, str]] = []
    for item in items[-6:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role and content:
            result.append({"role": role[:24], "content": content[:700]})
    return result


def _bounded_delivered(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    for item in items[-4:]:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())[:480]
        if text:
            result.append({"text": text, "terminal": bool(item.get("terminal"))})
    return result


def _narrator_system_prompt(inherited_prompt: Any, *, max_spoken_chars: int) -> str:
    prompt = str(inherited_prompt or "").strip()
    if not prompt:
        raise RuntimeError("AUIP narrator requires the inherited main-chat role prompt")
    clean_limit = max(24, int(max_spoken_chars))
    return (
        f"{prompt}\n\n{AUIP_NARRATOR_SYSTEM_PROMPT}\n"
        f"The display_text must be one spoken sentence and no more than {clean_limit} Unicode characters."
    )


def _structured_presenter_system_prompt(
    inherited_prompt: Any,
    *,
    max_spoken_chars: int,
    presentation_required: bool,
    display_language: str,
) -> str:
    prompt = str(inherited_prompt or "").strip()
    if not prompt:
        raise RuntimeError("AUIP presenter requires the inherited main-chat role prompt")
    addon = (
        AUIP_STRUCTURED_REQUIRED_PROMPT
        if presentation_required
        else AUIP_STRUCTURED_PRESENTATION_PROMPT
    )
    clean_limit = max(24, int(max_spoken_chars))
    return (
        f"{prompt}\n\n{addon}\n"
        f"The display_text must be one spoken sentence and no more than "
        f"{clean_limit} Unicode characters.\n"
        f"FINAL OUTPUT CONTRACT: display_text must be written in "
        f"{_display_language(display_language)}. This overrides the language "
        f"of every user topic, app string, and fact value in the input."
    )


def _display_language(value: Any) -> str:
    language = str(value or "simplified_chinese").strip().lower().replace("-", "_")
    if language in {"ja", "jp", "japanese"}:
        return "japanese"
    if language in {"zh", "zh_cn", "chinese", "simplified_chinese"}:
        return "simplified_chinese"
    return language[:40] or "simplified_chinese"


def _is_japanese(display_language: str) -> bool:
    return _display_language(display_language) == "japanese"
