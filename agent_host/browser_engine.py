from __future__ import annotations

from typing import Any, Protocol

from agent_host.provider_types import EmitProviderEvent, ProviderRunRequest, ProviderRunResult


class BrowserExecutionEngine(Protocol):
    """Low-level browser execution contract.

    Amadeus owns provider lifecycle, permission, branch memory, canvas, and
    narration. A browser engine only owns sessions and page actions. Current
    implementations can be Playwright-backed; future ones can wrap Obscura CDP,
    Obscura MCP, Stagehand, or another browser automation core without changing
    ProviderRuntime or canvas/observer semantics.
    """

    provider_id: str
    engine_id: str

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> ProviderRunResult:
        ...

    async def inspect_session(self, session_id: str, *, include_dom: bool = True) -> dict[str, Any]:
        """Return high-detail session state for hidden provider branches."""
        ...

    async def cancel(self, run_id: str) -> None:
        ...

    async def shutdown(self) -> None:
        ...
