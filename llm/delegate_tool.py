"""The delegate contract as a schema instead of as prose.

DELEGATE has always been a tool call in everything but transport: the model
states a structured intent, the host executes it, the result comes back. It
was expressed as an inline tag parsed out of the text stream, which meant
every constraint a schema would enforce for free — provider is required,
provider is registered, and either a text task or structured operation must
be complete — could only be asked for in sentences, and asking is not
enforcing. On 2026-07-31 that gap accounted for the entire class of failures
under investigation.

The schema below is deliberately the same vocabulary the tag already used, so
the host side downstream of `record_actions` is unchanged and the two
transports remain interchangeable behind `LLM_DELEGATE_TOOL_CALLS`.

Measured later that same day, swapping the transport lost. Native tool calls
took the read-only violation rate to 58% where the tag sat at 20% -- tool
calling is a post-trained reflex, and that reflex does not know this system's
read-only invariant -- while a turn-terminating call also couples how long the
character speaks to whether the work dispatches at all. The win came from
applying schema where the constraint actually is: a required `intent`
attribute, which took that same step to 8/8 clean. So the flag above is a
reproducible comparison, not a rollout awaiting its flip; read
docs/delegate_transport_toolcall_probe_2026-07-31.md before changing it.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

DELEGATE_TOOL_NAME = "delegate"

# Attributes carried over verbatim from the inline tag vocabulary. Keeping the
# names identical is what lets the tag and the tool call share every consumer
# below record_actions.
DELEGATE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": DELEGATE_TOOL_NAME,
        "description": (
            "Carry out a request that needs a provider, or set the current "
            "conversation project. A pure project switch uses intent=focus; "
            "a switch combined with another operation keeps that operation's "
            "intent and uses the focus modifier. Say a short line to the user "
            "in the same reply before calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": (
                        "Use one of the exact provider ids registered in the "
                        "current runtime and described by Provider routing."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "Complete instruction: what to do and how. Never a bare "
                        "noun or location. Describes only the user's request - "
                        "never your own persona, name or background."
                    ),
                },
                "intent": {
                    "type": "string",
                    "enum": ["execute", "report", "amend", "retract", "focus"],
                    "description": (
                        "execute when a Provider must start a new unit of work, including "
                        "read-only observation or analysis of current external state; "
                        "amend when they asked for any follow-up action grounded "
                        "in an existing task or its outputs, including edit, validate, "
                        "read, summarize, analyze, audit, copy, move or delete, even "
                        "when read-only (name every target file in task); "
                        "report only when existing Work Ledger facts can answer an "
                        "existing task or project's status or result; retract when they "
                        "asked to stop work that is already running; focus when "
                        "they set or clear the project for this conversation."
                    ),
                },
                "subject": {
                    "type": "string",
                    "enum": ["work_item", "project"],
                    "description": (
                        "Required with intent=report: work_item for a task or "
                        "artifact status; project for project status or a recent-"
                        "projects list. Omitted legacy reports default to work_item."
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": (
                        "Exact known project id. With intent=focus or focus=set, "
                        "omit this only when returning the conversation to Drafts."
                    ),
                },
                "focus": {
                    "type": "string",
                    "enum": ["set", "clear"],
                    "description": (
                        "Optional persistent conversation-destination modifier. "
                        "Use set with project_id, or clear to return future work "
                        "to Drafts, only when the user explicitly changes where "
                        "subsequent turns belong. Naming a project or existing task "
                        "as this operation's target does not imply focus; a cross-project "
                        "amend uses project_id/workspace_ref without focus unless the user "
                        "also explicitly asks to switch. Keep intent as the actual operation "
                        "(execute, amend, report, or retract). A pure switch instead uses "
                        "intent=focus without this modifier."
                    ),
                },
                "one_off": {
                    "type": "boolean",
                    "description": (
                        "True only when the user explicitly says this one task "
                        "is separate/one-off. It uses Drafts without changing "
                        "the conversation's current project."
                    ),
                },
                "target": {
                    "type": "string",
                    "enum": ["desktop"],
                    "description": (
                        "Set when the requested final destination is outside "
                        "the workspace (Desktop today). The selected workspace "
                        "provider still builds inside its workspace; Amadeus "
                        "stages and exports to the destination after approval. "
                        "When amending a previously delivered Desktop file, "
                        "also use intent=amend and subject=work_item so its "
                        "export-owning WorkItem is continued. "
                        "Omit for ordinary in-workspace work."
                    ),
                },
                "workspace_ref": {
                    "type": "string",
                    "description": (
                        "work_item_id of the existing task this changes. Omit "
                        "for new work."
                    ),
                },
                "cwd": {"type": "string", "description": "Explicit project directory."},
                "branch": {
                    "type": "string",
                    "enum": ["continue", "new", "close"],
                    "description": "Browser branch intent.",
                },
                "action": {
                    "type": "string",
                    "enum": ["open", "search", "click_text", "observe"],
                    "description": (
                        "Browser action. open is one atomic navigation and "
                        "requires url. Use search only to search inside the "
                        "current page/site; include query when it is explicit. "
                        "For open-ended web finding without an exact URL, omit "
                        "action and describe the complete task."
                    ),
                },
                "url": {"type": "string"},
                "query": {
                    "type": "string",
                    "description": "Exact current-page search query for action=search.",
                },
                "text": {"type": "string", "description": "Visible link text to click."},
            },
            "required": ["provider"],
            "anyOf": [
                {"required": ["task"]},
                {
                    "properties": {"intent": {"const": "focus"}},
                    "required": ["intent"],
                },
                {
                    "properties": {
                        "intent": {"enum": ["execute", "amend"]},
                    },
                    "required": ["intent", "action"],
                },
            ],
        },
    },
}


def delegate_tool_for_registered_providers() -> dict[str, Any]:
    """Narrow the schema enum to the same live vocabulary as the prompt."""

    from llm.prompts import registered_provider_ids

    tool = deepcopy(DELEGATE_TOOL)
    provider_ids = list(registered_provider_ids())
    tool["function"]["parameters"]["properties"]["provider"]["enum"] = provider_ids
    return tool


class ToolCallAccumulator:
    """Reassemble tool calls that arrive as streamed deltas.

    Arguments stream as fragments and several calls may interleave by index,
    so nothing can be dispatched until the stream ends — unlike the inline
    tag, which is dispatchable the moment it closes.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def feed(self, tool_calls: Any) -> None:
        for call in tool_calls or []:
            index = int(getattr(call, "index", 0) or 0)
            slot = self._calls.setdefault(index, {"name": "", "arguments": ""})
            function = getattr(call, "function", None)
            name = getattr(function, "name", None)
            if name:
                slot["name"] = str(name)
            arguments = getattr(function, "arguments", None)
            if arguments:
                slot["arguments"] += str(arguments)

    def actions(self) -> list[dict[str, Any]]:
        """Return DELEGATE actions in the same shape the tag parser produces."""

        out: list[dict[str, Any]] = []
        for _index, slot in sorted(self._calls.items()):
            if slot["name"] != DELEGATE_TOOL_NAME:
                continue
            try:
                parsed = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            attrs = {
                key: str(value)
                for key, value in parsed.items()
                if isinstance(key, str) and value not in (None, "")
            }
            intent = str(attrs.get("intent") or "").strip().lower()
            if not attrs.get("provider") or (
                not attrs.get("task")
                and intent != "focus"
                and not (
                    attrs.get("action")
                    and intent in {"execute", "amend"}
                )
            ):
                continue
            out.append({"type": "DELEGATE", "attrs": attrs, "raw": as_tag_text(attrs)})
        return out


def as_tag_text(attrs: dict[str, Any]) -> str:
    """Render a call in the tag syntax, for the transcript the model re-reads.

    History has to show the assistant delegating rather than merely promising:
    a turn recorded without its call teaches the model, by its own strongest
    in-context example, that file work is answered with words alone. Until the
    history itself carries native tool-call messages, the tag form is what the
    model already understands there.
    """

    ordered = ["provider", *(k for k in attrs if k not in {"provider", "task"}), "task"]
    body = " ".join(
        f'{key}="{str(attrs[key])}"' for key in ordered if attrs.get(key) not in (None, "")
    )
    return f"[DELEGATE {body}]"
