from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from agent_host.provider_catalog import BROWSER_MANIFEST
from agent_host.provider_contract import ProviderOperation
from agent_host.provider_types import ProviderRunRequest


BranchEntryKind = Literal["direct", "dom_branch", "dom_branch_observe_first"]
BranchMergeStrategy = Literal["compact_visible_merge"]


@dataclass(slots=True, frozen=True)
class BrowserBranchPolicyDecision:
    use_branch: bool
    entry_kind: BranchEntryKind
    initial_request: ProviderRunRequest
    max_actions: int
    capture_hidden_dom: bool
    merge_strategy: BranchMergeStrategy
    reason: str

    def to_metadata(self) -> dict[str, Any]:
        return {
            "use_branch": self.use_branch,
            "entry_kind": self.entry_kind,
            "max_actions": self.max_actions,
            "capture_hidden_dom": self.capture_hidden_dom,
            "merge_strategy": self.merge_strategy,
            "reason": self.reason,
        }


class BrowserInteractionPolicy:
    """Decide when browser work may enter a hidden DOM branch.

    Full DOM is a short-lived provider branch context, not durable chat memory.
    Main chat may request browser work, but runtime policy owns the transition
    into and out of high-detail DOM state.
    """

    OBSERVE_ACTIONS = {"observe", "snapshot", "extract"}

    def __init__(self, *, branch_enabled_env: str = "AMADEUS_BROWSER_BRANCH_ENABLED") -> None:
        self.branch_enabled_env = branch_enabled_env

    def decide(self, request: ProviderRunRequest) -> BrowserBranchPolicyDecision:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        action = self._action(request)
        operation = self._operation(action)
        max_actions = self.max_actions(metadata)
        explicit_branch = _truthy(metadata.get("provider_branch"))
        source = str(metadata.get("source") or "").strip().lower()

        if not self.branch_enabled():
            return self._direct(request, max_actions=max_actions, reason="branch_disabled")

        if explicit_branch:
            return self._branch_decision(
                request,
                action=action,
                max_actions=max_actions,
                reason="explicit_provider_branch",
            )

        if source != "llm_delegate":
            return self._direct(request, max_actions=max_actions, reason="non_llm_delegate")

        if max_actions == 0 and not self._requires_observation(operation):
            return self._direct(request, max_actions=max_actions, reason="no_branch_actions_requested")

        if self._requires_observation(operation):
            return self._branch_decision(
                request,
                action=action,
                max_actions=max_actions,
                reason="dom_action_requires_hidden_page_state",
            )

        if action in self.OBSERVE_ACTIONS and self._has_browser_session(metadata):
            return self._branch_decision(
                request,
                action=action,
                max_actions=max_actions,
                reason="current_page_followup_requires_hidden_page_state",
            )

        if action == "open" and self._open_task_needs_page_interaction(request.task):
            return self._branch_decision(
                request,
                action=action,
                max_actions=max_actions,
                reason="open_then_interact_task_requires_hidden_page_state",
            )

        return self._direct(request, max_actions=max_actions, reason="simple_browser_action")

    def branch_enabled(self) -> bool:
        return os.environ.get(self.branch_enabled_env, "1").strip().lower() not in {"0", "false", "no", "off"}

    def max_actions(self, metadata: dict[str, Any]) -> int:
        return _bounded_int(metadata.get("max_branch_actions"), default=3, low=0, high=8)

    def _branch_decision(
        self,
        request: ProviderRunRequest,
        *,
        action: str,
        max_actions: int,
        reason: str,
    ) -> BrowserBranchPolicyDecision:
        observe_first = self._requires_observation(self._operation(action))
        return BrowserBranchPolicyDecision(
            use_branch=True,
            entry_kind="dom_branch_observe_first" if observe_first else "dom_branch",
            initial_request=self._observe_first_request(request) if observe_first else request,
            max_actions=max_actions,
            capture_hidden_dom=True,
            merge_strategy="compact_visible_merge",
            reason=reason,
        )

    @staticmethod
    def _direct(request: ProviderRunRequest, *, max_actions: int, reason: str) -> BrowserBranchPolicyDecision:
        return BrowserBranchPolicyDecision(
            use_branch=False,
            entry_kind="direct",
            initial_request=request,
            max_actions=max_actions,
            capture_hidden_dom=False,
            merge_strategy="compact_visible_merge",
            reason=reason,
        )

    @staticmethod
    def _observe_first_request(request: ProviderRunRequest) -> ProviderRunRequest:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        next_metadata = dict(metadata)
        next_metadata["browser_action"] = "observe"
        next_metadata["browser_mode"] = "observe"
        next_metadata["branch_original_action"] = str(
            metadata.get("browser_action") or metadata.get("action") or request.mode or ""
        ).strip().lower()
        return ProviderRunRequest(
            provider=request.provider,
            task=f"Observe before browser branch action: {request.task}",
            cwd=request.cwd,
            mode="observe",
            metadata=next_metadata,
        )

    @staticmethod
    def _action(request: ProviderRunRequest) -> str:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        return str(metadata.get("browser_action") or metadata.get("action") or request.mode or "").strip().lower()

    @staticmethod
    def _operation(action: str) -> ProviderOperation | None:
        return BROWSER_MANIFEST.capabilities.operation(action)

    @staticmethod
    def _requires_observation(operation: ProviderOperation | None) -> bool:
        return bool(operation is not None and operation.execution == "observe_then_plan")

    @staticmethod
    def _has_browser_session(metadata: dict[str, Any]) -> bool:
        return bool(str(metadata.get("browser_session_id") or metadata.get("browserSessionId") or "").strip())

    @staticmethod
    def _open_task_needs_page_interaction(task: str) -> bool:
        text = str(task or "").strip().lower()
        if not text:
            return False
        patterns = (
            r"\bthen\b",
            r"\bclick\b",
            r"\bfill\b",
            r"\btype\b",
            r"\bsubmit\b",
            r"\bselect\b",
            r"\bsearch box\b",
            r"\bsearch button\b",
            "\u70b9\u51fb",
            "\u70b9\u5f00",
            "\u8f93\u5165",
            "\u586b\u5199",
            "\u63d0\u4ea4",
            "\u9009\u62e9",
            "\u641c\u7d22\u6846",
            "\u641c\u7d22\u6309\u94ae",
            "\u7136\u540e",
            "\u7ee7\u7eed\u64cd\u4f5c",
            "\u691c\u7d22",
            "\u30af\u30ea\u30c3\u30af",
            "\u5165\u529b",
            "点击",
            "点开",
            "输入",
            "填写",
            "提交",
            "选择",
            "搜索框",
            "搜索按钮",
            "然后",
            "继续操作",
        )
        return any(re.search(pattern, text) if pattern.startswith(r"\b") else pattern in text for pattern in patterns)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(high, parsed))
