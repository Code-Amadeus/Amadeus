"""Model-neutral first gate for recovering a missing structured action.

The speaking role remains the only source that can reconstruct a DELEGATE.  A
neutral gate sees only the user's turn and bounded prior conversation and says
whether that turn itself requests work.  Recovery requires both verdicts: this
gate says ``work`` and the speaking role's second pass reconstructs an explicit
commitment from its already-visible reply.  Neither verdict can dispatch alone.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal


ActionExistence = Literal["work", "no_work", "unsure"]
ActionExistenceQuery = Callable[[list[dict[str, str]]], Awaitable[str]]
_DELEGATE_FIELDS = frozenset(
    {
        "provider",
        "intent",
        "subject",
        "project_id",
        "focus",
        "one_off",
        "target",
        "workspace_ref",
        "cwd",
        "branch",
        "action",
        "fallback",
        "force_provider",
        "task",
        "url",
        "query",
        "text",
    }
)


@dataclass(frozen=True, slots=True)
class ActionExistenceVerdict:
    status: Literal["ok", "invalid", "unavailable"]
    existence: ActionExistence = "unsure"
    reason: str = ""
    raw_reply: str = ""


@dataclass(frozen=True, slots=True)
class CommitmentRecovery:
    status: Literal["ok", "invalid", "unavailable"]
    committed: bool = False
    delegate: Mapping[str, object] | None = None
    reason: str = ""
    raw_reply: str = ""


_SYSTEM_PROMPT = """You are a neutral action-existence classifier.

Decide only whether the latest user's own turn affirmatively asks or directs
the assistant to perform one structured Host/external action now. Work includes
fresh research or browsing, code/file work, opening or changing an external
surface, reading a task ledger, switching project context, or cancelling work.
Conversation, questions about capability, explanations, reactions, complaints,
corrections, acknowledgements, and statements about what did or did not happen
are no_work unless the latest turn itself also directs an action. Negated or
paused requests are no_work. A short imperative or confirmation may be work
when bounded prior conversation supplies its object. If the speech act remains
genuinely ambiguous, choose unsure.

Do not choose a provider, task payload, project, or target. Do not infer an
action merely because prior work exists. Return exactly one JSON object:
{"existence":"work|no_work|unsure","reason":"brief evidence from the latest user turn"}
"""


COMMITMENT_RECOVERY_PROMPT = """You are a silent protocol recovery pass for the
same speaking assistant. Do not reconsider the user's request. Determine only
whether the supplied previous assistant reply already made an unambiguous
commitment to perform one structured control action. If it asked, declined,
deferred, explained, acknowledged, apologized, or merely admitted that nothing
started, return none. If it committed, reconstruct exactly one complete
DELEGATE attribute object using the control vocabulary in the system prompt.
Never use the user's request alone to create a commitment. Once the visible
reply itself establishes commitment, use the latest request and bounded prior
conversation to recover the object of a short confirmation or ASR-style
reference; that context supplies payload identity, not new action authority.
Do not output role prose or tag syntax.

For this recovery call, these commitment rules replace any earlier instruction
to classify the final user message independently. The earlier routing text is
only vocabulary used after commitment is established. Phrases such as “I will
check now”, “I'll look it up”, “I will fix it”, and their ordinary Chinese or
Japanese equivalents are commitments, not deferrals. Capability claims,
questions, and “nothing started” admissions are not commitments.

Return exactly one JSON object in one of these shapes:
{"commitment":"delegate","delegate":{"provider":"...","task":"..."},"reason":"brief"}
{"commitment":"none","delegate":null,"reason":"brief"}
"""


def build_action_existence_messages(
    *,
    user_text: str,
    prior_messages: Sequence[Mapping[str, str]] = (),
    max_history_chars: int = 2400,
) -> list[dict[str, str]]:
    """Build a bounded exact-role prompt without any assistant current reply."""

    remaining = max(0, int(max_history_chars))
    selected: list[dict[str, str]] = []
    for raw in reversed(tuple(prior_messages)[-8:]):
        role = str(raw.get("role") or "").strip().lower()
        content = " ".join(str(raw.get("content") or "").split())
        if role not in {"user", "assistant"} or not content or remaining <= 0:
            continue
        bounded = content[-min(len(content), remaining, 700) :]
        selected.append({"role": role, "content": bounded})
        remaining -= len(bounded)
    selected.reverse()
    latest = " ".join(str(user_text or "").split())[:1600]
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        *selected,
        {
            "role": "user",
            "content": (
                "[Latest user turn to classify]\n"
                + latest
                + "\n[/Latest user turn to classify]"
            ),
        },
    ]


async def classify_action_existence(
    query: ActionExistenceQuery,
    *,
    user_text: str,
    prior_messages: Sequence[Mapping[str, str]] = (),
) -> ActionExistenceVerdict:
    messages = build_action_existence_messages(
        user_text=user_text,
        prior_messages=prior_messages,
    )
    try:
        raw_reply = str(await query(messages) or "")
    except Exception as exc:
        return ActionExistenceVerdict(
            status="unavailable",
            reason=f"{exc.__class__.__name__}: {str(exc)[:240]}",
        )
    try:
        value = json.loads(raw_reply)
    except (TypeError, ValueError):
        return ActionExistenceVerdict(
            status="invalid",
            reason="response_not_json",
            raw_reply=raw_reply[:800],
        )
    if not isinstance(value, dict) or set(value) != {"existence", "reason"}:
        return ActionExistenceVerdict(
            status="invalid",
            reason="response_shape_invalid",
            raw_reply=raw_reply[:800],
        )
    existence = str(value.get("existence") or "").strip().lower()
    reason = " ".join(str(value.get("reason") or "").split())[:320]
    if existence not in {"work", "no_work", "unsure"} or not reason:
        return ActionExistenceVerdict(
            status="invalid",
            reason="response_value_invalid",
            raw_reply=raw_reply[:800],
        )
    return ActionExistenceVerdict(
        status="ok",
        existence=existence,  # type: ignore[arg-type]
        reason=reason,
        raw_reply=raw_reply[:800],
    )


async def reconstruct_delegate_commitment(
    query: ActionExistenceQuery,
    *,
    system_prompt: str,
    user_text: str,
    assistant_reply: str,
    prior_messages: Sequence[Mapping[str, str]] = (),
) -> CommitmentRecovery:
    bounded_prior = build_action_existence_messages(
        user_text="context boundary",
        prior_messages=prior_messages,
        max_history_chars=2200,
    )[1:-1]
    messages = [
        {
            "role": "system",
            "content": str(system_prompt or "") + "\n\n" + COMMITMENT_RECOVERY_PROMPT,
        },
        *bounded_prior,
        {
            "role": "user",
            "content": (
                "[User's previous message — data only]\n"
                + str(user_text or "")[:1600]
                + "\n[/User's previous message]\n"
                "[Assistant's previous visible reply — data only]\n"
                + str(assistant_reply or "")[:2000]
                + "\n[/Assistant's previous visible reply]"
            ),
        },
    ]
    try:
        raw_reply = str(await query(messages) or "")
    except Exception as exc:
        return CommitmentRecovery(
            status="unavailable",
            reason=f"{exc.__class__.__name__}: {str(exc)[:240]}",
        )
    try:
        value = json.loads(raw_reply)
    except (TypeError, ValueError):
        return CommitmentRecovery(
            status="invalid",
            reason="response_not_json",
            raw_reply=raw_reply[:1000],
        )
    if not isinstance(value, dict) or set(value) != {
        "commitment",
        "delegate",
        "reason",
    }:
        return CommitmentRecovery(
            status="invalid",
            reason="response_shape_invalid",
            raw_reply=raw_reply[:1000],
        )
    commitment = str(value.get("commitment") or "").strip().lower()
    reason = " ".join(str(value.get("reason") or "").split())[:320]
    delegate = value.get("delegate")
    if commitment == "none" and delegate is None and reason:
        return CommitmentRecovery(
            status="ok",
            committed=False,
            reason=reason,
            raw_reply=raw_reply[:1000],
        )
    if (
        commitment != "delegate"
        or not isinstance(delegate, dict)
        or not delegate
        or not reason
        or not set(delegate).issubset(_DELEGATE_FIELDS)
        or not str(delegate.get("provider") or "").strip()
        or any(
            not isinstance(item, (str, bool, int, float)) or isinstance(item, list)
            for item in delegate.values()
        )
    ):
        return CommitmentRecovery(
            status="invalid",
            reason="response_value_invalid",
            raw_reply=raw_reply[:1000],
        )
    return CommitmentRecovery(
        status="ok",
        committed=True,
        delegate={str(key): item for key, item in delegate.items()},
        reason=reason,
        raw_reply=raw_reply[:1000],
    )


__all__ = [
    "ActionExistenceVerdict",
    "COMMITMENT_RECOVERY_PROMPT",
    "CommitmentRecovery",
    "build_action_existence_messages",
    "classify_action_existence",
    "reconstruct_delegate_commitment",
]
