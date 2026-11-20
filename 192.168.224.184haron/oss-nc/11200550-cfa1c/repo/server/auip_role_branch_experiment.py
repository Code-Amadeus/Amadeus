"""Reversible contracts for the AppSession role-branch experiment.

The shipping AUIP path deliberately keeps role speech, Participant selection,
and application execution separate. ``AppSessionRoleBranch`` is used only by
the default-off A1 memory-placement sidepath; it does not change that topology.
The module also defines two product-inert counterfactual decision arms:

* ``role_executor``: one branch-scoped role call returns speech and one exact
  typed application proposal;
* ``participant_first``: a narrow role-free Participant sees accepted state,
  action schemas, and the user's downlink instruction, then the role speaks
  from that single preflighted proposal without receiving raw state JSON.

The Host still owns AppSession identity, revision, legality, invocation, and
receipt truth. Importing this module has no side effects; production creates a
branch only when ``AUIP_APPSESSION_ROLE_BRANCH_MODE=a1``.
"""

from __future__ import annotations

import copy
import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

from server.auip_contract import AuipProtocolError, validate_payload


_BLOCKED_TOOL = "auip_branch_blocked"
_WAIT_TOOL = "auip_branch_wait"
_ACTION_PREFIX = "auip_branch_action_"
_INSTRUCTION_RELATIONS = frozenset(
    {"follows", "not_applicable", "safe_alternative"}
)
_STATE_KEYS = frozenset(
    {
        "state",
        "current_state",
        "board",
        "turn",
        "binding",
        "rolebindings",
        "controls",
    }
)


@dataclass(frozen=True, slots=True)
class AppSessionBranchMessage:
    """One bounded visible or Host-verified fact in an AppSession branch."""

    role: str
    content: str
    kind: str = "dialogue"


@dataclass(frozen=True, slots=True)
class AppSessionBranchProposal:
    """One model proposal before Host preflight or application execution."""

    action: str
    action_type: str = ""
    payload: dict[str, Any] | None = None
    instruction_relation: str = ""
    choice_reason: str = ""
    semantic_label: str = ""
    speech: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", dict(self.payload or {}))


class AppSessionRoleBranch:
    """Small host-managed dialogue memory keyed by one AppSession identity."""

    def __init__(
        self,
        *,
        app_session_id: str,
        app_title: str,
        max_messages: int = 12,
        max_chars: int = 2600,
        checkpoint_messages: list[Mapping[str, Any]] | None = None,
    ) -> None:
        clean_id = str(app_session_id or "").strip()
        if not clean_id:
            raise ValueError("app_session_id is required")
        self.app_session_id = clean_id
        self.app_title = str(app_title or "application").strip() or "application"
        self.max_messages = max(4, int(max_messages))
        self.max_chars = max(400, int(max_chars))
        self._messages: deque[AppSessionBranchMessage] = deque()
        self._verified_receipts: deque[dict[str, Any]] = deque(maxlen=8)
        self._capsule: dict[str, Any] | None = None
        for message in checkpoint_messages or []:
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            self._append(role, str(message.get("content") or ""), "checkpoint")

    @property
    def active(self) -> bool:
        return self._capsule is None

    @property
    def capsule(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._capsule)

    def record_user(self, text: str) -> None:
        """Record ordinary branch dialogue without making it durable strategy."""

        self._append("user", text, "dialogue")

    def record_strategy_directive(self, text: str) -> None:
        """Record a session-scoped strategy downlink selected by the router."""

        self._append("user", text, "strategy_directive")

    def record_assistant(self, text: str) -> None:
        self._append("assistant", text, "dialogue")

    def record_narration(self, text: str) -> None:
        """Record only role narration that the shared delivery sink accepted."""

        self._append("assistant", text, "narration")

    def record_receipt(
        self,
        *,
        accepted: bool,
        action_type: str,
        payload: Mapping[str, Any] | None,
        resulting_revision: int | None = None,
        reason: str = "",
    ) -> None:
        fact = {
            "accepted": bool(accepted),
            "action_type": str(action_type or "").strip(),
            "payload": validate_payload(payload or {}),
            **(
                {"resulting_revision": int(resulting_revision)}
                if resulting_revision is not None
                else {}
            ),
            **({"reason": str(reason or "").strip()[:240]} if reason else {}),
        }
        self._verified_receipts.append(copy.deepcopy(fact))
        self._append(
            "system",
            "[Verified AUIP receipt] "
            + json.dumps(fact, ensure_ascii=False, separators=(",", ":")),
            "receipt",
        )

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": item.role, "content": item.content}
            for item in self._messages
        ]

    def recent_user_directives(self, *, limit: int = 2) -> list[str]:
        return [
            item.content
            for item in self._messages
            if item.role == "user" and item.kind == "strategy_directive"
        ][-max(1, int(limit)) :]

    def collapse(
        self,
        *,
        close_status: str,
        close_reason: str = "",
        terminal: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Collapse active high-resolution dialogue into one parent capsule."""

        if self._capsule is not None:
            return copy.deepcopy(self._capsule)
        dialogue_tail = [
            {"role": item.role, "content": item.content}
            for item in self._messages
            if item.kind in {"dialogue", "strategy_directive", "narration"}
        ][-4:]
        capsule = {
            "kind": "auip_appsession_branch_capsule/v1",
            "app_session_id": self.app_session_id,
            "app_title": self.app_title,
            "close_status": str(close_status or "closed").strip()[:80],
            "close_reason": str(close_reason or "").strip()[:240],
            "dialogue_tail": dialogue_tail,
            "strategy_directives": self.recent_user_directives(limit=3),
            "verified_actions": list(self._verified_receipts)[-4:],
            **(
                {
                    "terminal": {
                        key: copy.deepcopy(terminal.get(key))
                        for key in ("type", "winner", "outcome", "reason")
                        if terminal.get(key) not in (None, "")
                    }
                }
                if isinstance(terminal, Mapping)
                else {}
            ),
        }
        self._capsule = capsule
        self._messages.clear()
        return copy.deepcopy(capsule)

    def render_role_context(self, *, max_chars: int = 2200) -> str:
        """Render bounded branch memory as current-turn evidence, not authority.

        The ordinary provider history is intentionally absent on an A1 app
        turn.  This block restores only the dialogue local to the active
        AppSession.  Verified receipt rows are Host facts; user and assistant
        dialogue remain conversational evidence and can never prove execution.
        """

        if self._capsule is not None or not self._messages:
            return ""
        limit = max(400, int(max_chars))
        rows = [
            {
                "role": item.role,
                "kind": item.kind,
                "content": item.content,
            }
            for item in self._messages
        ]
        opening = "\n".join(
            [
                "[Active AUIP AppSession dialogue branch]",
                "This is bounded dialogue local to the focused AppSession. "
                "Only rows marked receipt are Host-verified execution facts; "
                "all other rows are conversation or delivered presentation. "
                "Use them for continuity, never as action authority, current "
                "legality, or proof that an intended action completed.",
            ]
        )
        closing = "[/Active AUIP AppSession dialogue branch]"
        budget = max(80, limit - len(opening) - len(closing) - 2)
        while rows:
            encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) <= budget:
                return f"{opening}\n{encoded}\n{closing}"
            rows.pop(0)
        return ""

    def _append(self, role: str, text: str, kind: str) -> None:
        if self._capsule is not None:
            raise AuipProtocolError("role_branch_closed")
        clean = str(text or "").strip()
        if not clean:
            return
        self._messages.append(
            AppSessionBranchMessage(
                role=str(role or "").strip().lower(),
                content=clean[:1600],
                kind=kind,
            )
        )
        while len(self._messages) > self.max_messages:
            self._messages.popleft()
        while self._messages and sum(
            len(item.content) for item in self._messages
        ) > self.max_chars:
            self._messages.popleft()


def participant_first_input(
    participant_context: Mapping[str, Any],
    *,
    user_instruction: str = "",
    branch: AppSessionRoleBranch,
    trigger: str = "participant_opportunity",
    action_required: bool = True,
) -> dict[str, Any]:
    """Project a narrow automatic opportunity with optional user downlink."""

    source = dict(participant_context or {})
    return {
        key: copy.deepcopy(source.get(key))
        for key in (
            "app",
            "revision",
            "state",
            "available_actions",
            "available_choice_options",
            "choice_action_types",
            "recent_verified_self_actions",
            "recent_semantic_beats",
        )
        if source.get(key) is not None
    } | {
        "action_required": bool(action_required),
        "trigger": str(trigger or "participant_opportunity").strip()[:120],
        "user_instruction": str(user_instruction or "").strip()[:1000],
        "strategy_directives": branch.recent_user_directives(limit=2),
    }


def participant_first_tools(
    available_actions: Mapping[str, Any],
    *,
    action_required: bool = True,
    available_choice_options: list[Mapping[str, Any]] | None = None,
    choice_action_types: list[str] | tuple[str, ...] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build exact-payload tools plus a non-authoritative choice explanation."""

    tools: list[dict[str, Any]] = [
        _outcome_tool(
            _BLOCKED_TOOL if action_required else _WAIT_TOOL,
            blocked=action_required,
            include_speech=False,
        )
    ]
    action_by_tool: dict[str, Any] = {}
    governed = {
        str(value or "").strip().lower()
        for value in (choice_action_types or [])
        if str(value or "").strip()
    }
    for index, option in enumerate(available_choice_options or []):
        if not isinstance(option, Mapping):
            continue
        action_type = str(option.get("action") or "").strip().lower()
        if not action_type or action_type not in governed:
            continue
        label = str(option.get("label") or action_type).strip()[:160]
        payload = validate_payload(option.get("payload") or {})
        tool_name = f"auip_branch_choice_{index}"
        tools.append(
            _choice_tool(
                tool_name,
                label=label,
                include_speech=False,
            )
        )
        action_by_tool[tool_name] = {
            "action_type": action_type,
            "payload": payload,
            "semantic_label": label,
        }
    for index, (raw_type, raw_spec) in enumerate(
        sorted(dict(available_actions or {}).items())
    ):
        action_type = str(raw_type or "").strip().lower()
        if not action_type or action_type in governed:
            continue
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        payload_schema = spec.get("inputSchema")
        if not isinstance(payload_schema, Mapping):
            payload_schema = {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            }
        tool_name = f"{_ACTION_PREFIX}{index}"
        tools.append(
            _action_tool(
                tool_name,
                description=str(spec.get("description") or action_type),
                payload_schema=dict(payload_schema),
                include_speech=False,
            )
        )
        action_by_tool[tool_name] = action_type
    return tools, action_by_tool


def role_executor_tools(
    available_actions: Mapping[str, Any],
    *,
    available_choice_options: list[Mapping[str, Any]] | None = None,
    choice_action_types: list[str] | tuple[str, ...] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build B-arm tools where speech and one exact payload share a decision."""

    tools = [_outcome_tool(_BLOCKED_TOOL, blocked=True, include_speech=True)]
    action_by_tool: dict[str, Any] = {}
    governed = {
        str(value or "").strip().lower()
        for value in (choice_action_types or [])
        if str(value or "").strip()
    }
    for index, option in enumerate(available_choice_options or []):
        if not isinstance(option, Mapping):
            continue
        action_type = str(option.get("action") or "").strip().lower()
        if not action_type or action_type not in governed:
            continue
        label = str(option.get("label") or action_type).strip()[:160]
        payload = validate_payload(option.get("payload") or {})
        tool_name = f"auip_branch_choice_{index}"
        tools.append(
            _choice_tool(
                tool_name,
                label=label,
                include_speech=True,
            )
        )
        action_by_tool[tool_name] = {
            "action_type": action_type,
            "payload": payload,
            "semantic_label": label,
        }
    for index, (raw_type, raw_spec) in enumerate(
        sorted(dict(available_actions or {}).items())
    ):
        action_type = str(raw_type or "").strip().lower()
        if not action_type or action_type in governed:
            continue
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        payload_schema = spec.get("inputSchema")
        if not isinstance(payload_schema, Mapping):
            payload_schema = {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            }
        tool_name = f"{_ACTION_PREFIX}{index}"
        tools.append(
            _action_tool(
                tool_name,
                description=str(spec.get("description") or action_type),
                payload_schema=dict(payload_schema),
                include_speech=True,
            )
        )
        action_by_tool[tool_name] = action_type
    return tools, action_by_tool


def parse_branch_tool_decision(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    action_by_tool: Mapping[str, Any],
    require_speech: bool,
    user_instruction: str | None = None,
) -> AppSessionBranchProposal:
    """Parse one B/C tool result without granting it execution authority."""

    name = str(tool_name or "").strip()
    args = dict(arguments or {})
    speech = str(args.get("speech") or "").strip()[:800]
    if require_speech and not speech:
        raise AuipProtocolError("invalid_role_branch_speech")
    if name in {_BLOCKED_TOOL, _WAIT_TOOL}:
        reason = str(args.get("reason") or "").strip()[:600]
        if not reason:
            raise AuipProtocolError("invalid_role_branch_reason")
        return AppSessionBranchProposal(
            action="blocked" if name == _BLOCKED_TOOL else "wait",
            choice_reason=reason,
            speech=speech,
        )
    mapped = action_by_tool.get(name)
    if isinstance(mapped, Mapping):
        action_type = str(mapped.get("action_type") or "").strip().lower()
        fixed_payload = validate_payload(mapped.get("payload") or {})
        semantic_label = str(mapped.get("semantic_label") or "").strip()[:160]
    else:
        action_type = str(mapped or "").strip().lower()
        fixed_payload = validate_payload(args.get("payload") or {})
        semantic_label = ""
    if not action_type:
        raise AuipProtocolError("invalid_role_branch_tool")
    has_instruction = bool(str(user_instruction or "").strip())
    relation = str(args.get("instruction_relation") or "").strip().lower()
    if user_instruction is not None and not has_instruction:
        # Automatic opportunity ownership is a Host fact, not semantic
        # authority delegated to the model. Ignore any echoed relation value.
        relation = "not_applicable"
    elif relation not in _INSTRUCTION_RELATIONS:
        raise AuipProtocolError("invalid_instruction_relation")
    elif user_instruction is not None and relation not in {
        "follows",
        "safe_alternative",
    }:
        raise AuipProtocolError("invalid_instruction_relation")
    reason = str(args.get("choice_reason") or "").strip()[:600]
    if not reason:
        raise AuipProtocolError("invalid_role_branch_reason")
    return AppSessionBranchProposal(
        action="act",
        action_type=action_type,
        payload=fixed_payload,
        instruction_relation=relation,
        choice_reason=reason,
        semantic_label=semantic_label,
        speech=speech,
    )


def role_presentation_payload(
    *,
    branch: AppSessionRoleBranch,
    app: Mapping[str, Any],
    user_instruction: str,
    proposal: AppSessionBranchProposal,
    action_description: str = "",
    receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build C-arm role input with proposal truth and no raw scene state."""

    bounded_receipt = None
    if receipt is not None:
        bounded_receipt = {
            key: copy.deepcopy(receipt.get(key))
            for key in (
                "accepted",
                "resulting_revision",
                "reason",
                "effects",
            )
            if receipt.get(key) is not None
        }
    payload = {
        "app": {
            key: copy.deepcopy(app.get(key))
            for key in ("title", "objective", "interactionSummary")
            if app.get(key) not in (None, "")
        },
        "branch_messages": branch.messages(),
        "user_instruction": str(user_instruction or "").strip()[:1000],
        "selected_outcome": {
            "action": proposal.action,
            "action_type": proposal.action_type,
            "payload": copy.deepcopy(proposal.payload or {}),
            "instruction_relation": proposal.instruction_relation,
            "choice_reason": proposal.choice_reason,
            "semantic_label": proposal.semantic_label,
            **(
                {"action_description": str(action_description or "").strip()[:320]}
                if action_description
                else {}
            ),
        },
        **({"receipt": bounded_receipt} if bounded_receipt is not None else {}),
    }
    if bounded_receipt is not None and _contains_state_key(
        bounded_receipt.get("effects")
    ):
        raise AuipProtocolError("role_branch_state_leak")
    return payload


def _action_tool(
    name: str,
    *,
    description: str,
    payload_schema: dict[str, Any],
    include_speech: bool,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "payload": copy.deepcopy(payload_schema),
        "instruction_relation": {
            "type": "string",
            "enum": sorted(_INSTRUCTION_RELATIONS),
            "description": (
                "not_applicable when this turn has no user downlink; follows when "
                "this implements the user's instruction; safe_alternative when "
                "accepted state requires a different legal choice"
            ),
        },
        "choice_reason": {
            "type": "string",
            "description": "Concise factual reason for this one proposal.",
        },
    }
    required = ["payload", "instruction_relation", "choice_reason"]
    if include_speech:
        properties["speech"] = {
            "type": "string",
            "description": (
                "Decisive in-character reply that promises only this proposal "
                "and does not claim a receipt already exists."
            ),
        }
        required.append("speech")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": str(description or "Propose this application action.")[:240],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _choice_tool(
    name: str,
    *,
    label: str,
    include_speech: bool,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "instruction_relation": {
            "type": "string",
            "enum": sorted(_INSTRUCTION_RELATIONS),
        },
        "choice_reason": {
            "type": "string",
            "description": (
                "Factual reason for selecting this exact Host-declared option. "
                "It must describe the same option named by this tool."
            ),
        },
    }
    required = ["instruction_relation", "choice_reason"]
    if include_speech:
        properties["speech"] = {
            "type": "string",
            "description": (
                "In-character reply committing only to the exact option named "
                "by this tool."
            ),
        }
        required.append("speech")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Select exactly this currently legal option: {label}"[:240],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _outcome_tool(
    name: str,
    *,
    blocked: bool,
    include_speech: bool,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "reason": {
            "type": "string",
            "description": (
                "Why no declared legal proposal can satisfy the required instruction."
                if blocked
                else "Why no application action is justified now."
            ),
        }
    }
    required = ["reason"]
    if include_speech:
        properties["speech"] = {
            "type": "string",
            "description": "Concise in-character explanation consistent with reason.",
        }
        required.append("speech")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Return a bounded non-action outcome.",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _contains_state_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key or "").strip().lower() in _STATE_KEYS:
                return True
            if _contains_state_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_state_key(item) for item in value)
    return False
