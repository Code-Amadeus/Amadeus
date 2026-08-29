"""Reusable product-path driver for AUIP natural-control Journeys."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import asyncio

from core.chat_runtime import ChatRuntime, _TurnState
from server.auip_control_decision import AuipControlDecisionResolver


RouteCallback = Callable[..., Awaitable[Any]]


class EmptyAuipLaunchCatalog:
    """Active-session Journeys do not expose unrelated launch candidates."""

    def candidates(self, _session_id: str, *, limit: int = 8) -> list[Any]:
        return []

    def preparation_candidates(
        self, _session_id: str, *, limit: int = 8
    ) -> list[Any]:
        return []


async def query_auip_control_model(
    messages: list[dict[str, str]],
    *,
    model: str,
) -> str:
    import llm.client as client

    return await asyncio.to_thread(
        client.remote_llm_messages_query,
        messages,
        temperature=0.0,
        max_tokens=300,
        timeout=60,
        model=model,
    )


@dataclass(slots=True)
class NaturalAuipControlDriver:
    """Exercise the same decision/dispatch seam used by a Chat turn.

    TTS and visible role generation are deliberately outside this driver. They
    do not own the action; the source-local decision and Host callback do.
    """

    conversation_id: str
    resolver: AuipControlDecisionResolver
    route: RouteCallback
    history: list[dict[str, str]] = field(default_factory=list)
    _runtime: ChatRuntime = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime = ChatRuntime()
        self._runtime.configure(
            auip_control_decider=self.resolver,
            auip_control_callback=self.route,
        )

    async def turn(
        self,
        utterance: str,
        *,
        turn_id: str,
        role_response: str = "わかったわ。",
    ) -> Any:
        state = _TurnState(
            gui_callback=None,
            turn_id=turn_id,
            question=utterance,
            session_id=self.conversation_id,
            control_prior_messages=self.history,
        )
        assert self._runtime._start_auip_decision(state) is True, (
            f"active AppSession did not put AUIP control in scope: {utterance!r}"
        )
        await self._runtime._wait_for_auip_role_grounding(state, timeout_s=3.5)
        # Production dispatch happens after or during visible role streaming.
        # Carry that same-turn decision into the Host seam so a Journey cannot
        # approve an action against stale prior chat while the speaking role
        # has just declined or changed it.
        wire_response = str(role_response or "").strip()
        # Exercise the same streaming parser as production. A matching inline
        # step may refine an already-authorized source-local decision, while
        # the visible response and retained history stay free of control tags.
        state.full_response = self._runtime._consume_stream_chunk(
            state,
            wire_response,
        ).strip()
        await self._runtime._wait_for_auip_controls(state)
        decision = state.auip_decision_result
        assert decision is not None
        self.history.extend(
            (
                {"role": "user", "content": utterance},
                {"role": "assistant", "content": state.full_response},
            )
        )
        return decision
