"""Provider branch store and merge contract.

A provider branch is a short-lived high-detail workspace for browser, coding,
or other tool-heavy tasks. It can hold raw DOM, terminal logs, diffs, and tool
traces without polluting the durable main chat. When the branch closes, a
BranchMergeRecord folds only user-visible interaction and compact evidence back
into the main conversation.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict


BranchVisibility = Literal["visible", "hidden"]
BranchRole = Literal["user", "assistant", "system", "tool"]


class BranchMessage(TypedDict):
    role: BranchRole
    content: str
    visibility: BranchVisibility
    source: str
    created_at: float
    metadata: dict[str, Any]


class BranchMergeRecord(TypedDict):
    branch_id: str
    parent_session_id: str
    provider: str
    status: str
    visible_messages: list[BranchMessage]
    hidden_message_count: int
    branch_store_path: str
    final_report: str
    compact_digest: str
    artifacts: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    risks: list[dict[str, Any]]
    next_state: dict[str, Any]
    created_at: float
    closed_at: float
    # Written by the browser branch adapter after a merge, and previously
    # absent from this definition -- the record's declared shape had drifted
    # behind the code that fills it. NotRequired because each is conditional:
    # `policy` is attached when an interaction policy decided the branch, and
    # the operation pair only when the manifest declares an outcome facet.
    policy: NotRequired[dict[str, Any]]
    operation: NotRequired[str]
    outcome_evidence: NotRequired[Any]


class ProviderBranch:
    def __init__(
        self,
        *,
        branch_id: str,
        parent_session_id: str,
        provider: str,
        goal: str,
        store_path: Path,
    ) -> None:
        self.branch_id = branch_id
        self.parent_session_id = parent_session_id
        self.provider = provider
        self.goal = goal
        self.store_path = store_path
        self.created_at = time.time()
        self.closed_at: float | None = None
        self.status = "running"
        self.messages: list[BranchMessage] = []
        self.artifacts: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.risks: list[dict[str, Any]] = []
        self.next_state: dict[str, Any] = {}

    def add_message(
        self,
        *,
        role: BranchRole,
        content: str,
        visibility: BranchVisibility,
        source: str = "branch",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not content:
            return
        self.messages.append(
            {
                "role": role,
                "content": str(content),
                "visibility": visibility,
                "source": str(source or "branch"),
                "created_at": time.time(),
                "metadata": dict(metadata or {}),
            }
        )

    def add_artifact(self, artifact: dict[str, Any]) -> None:
        if isinstance(artifact, dict):
            self.artifacts.append(dict(artifact))

    def add_action(self, action: dict[str, Any]) -> None:
        if isinstance(action, dict):
            self.actions.append(dict(action))

    def add_risk(self, risk: dict[str, Any]) -> None:
        if isinstance(risk, dict):
            self.risks.append(dict(risk))

    def close(
        self,
        *,
        final_report: str,
        compact_digest: str,
        status: str = "done",
        next_state: dict[str, Any] | None = None,
    ) -> BranchMergeRecord:
        self.status = status or "done"
        self.closed_at = time.time()
        self.next_state = dict(next_state or {})
        self._persist()
        visible_messages = [
            dict(item) for item in self.messages if item.get("visibility") == "visible"
        ]
        hidden_count = sum(1 for item in self.messages if item.get("visibility") == "hidden")
        return {
            "branch_id": self.branch_id,
            "parent_session_id": self.parent_session_id,
            "provider": self.provider,
            "status": self.status,
            "visible_messages": visible_messages,
            "hidden_message_count": hidden_count,
            "branch_store_path": str(self.store_path),
            "final_report": str(final_report or ""),
            "compact_digest": str(compact_digest or ""),
            "artifacts": [dict(item) for item in self.artifacts],
            "actions": [dict(item) for item in self.actions],
            "risks": [dict(item) for item in self.risks],
            "next_state": dict(self.next_state),
            "created_at": self.created_at,
            "closed_at": self.closed_at,
        }

    def _persist(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "branch_id": self.branch_id,
            "parent_session_id": self.parent_session_id,
            "provider": self.provider,
            "goal": self.goal,
            "status": self.status,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "messages": self.messages,
            "artifacts": self.artifacts,
            "actions": self.actions,
            "risks": self.risks,
            "next_state": self.next_state,
        }
        self.store_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class ProviderBranchStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._branches: dict[str, ProviderBranch] = {}

    def create_branch(
        self,
        *,
        parent_session_id: str,
        provider: str,
        goal: str,
        branch_id: str | None = None,
    ) -> ProviderBranch:
        bid = branch_id or f"{provider}_{uuid.uuid4().hex[:10]}"
        branch = ProviderBranch(
            branch_id=bid,
            parent_session_id=str(parent_session_id or ""),
            provider=str(provider or "provider"),
            goal=str(goal or ""),
            store_path=self.root / f"{bid}.json",
        )
        self._branches[bid] = branch
        return branch

    def get(self, branch_id: str) -> ProviderBranch | None:
        return self._branches.get(str(branch_id or ""))


def apply_branch_merge_to_history(
    merge: BranchMergeRecord,
    conversation_history: Any,
    *,
    include_final_report: bool = True,
) -> None:
    """Append only user-visible branch interaction to the main chat history."""
    for message in merge.get("visible_messages") or []:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if not content:
            continue
        if role == "user":
            conversation_history.add_user(content)
        elif role == "assistant":
            conversation_history.add_assistant(content)

    if include_final_report and merge.get("final_report"):
        conversation_history.add_assistant(
            f"[BRANCH_MERGE provider={merge.get('provider')} branch={merge.get('branch_id')}]\n"
            f"{merge.get('final_report')}"
        )
