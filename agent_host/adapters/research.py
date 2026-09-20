from __future__ import annotations

from agent_host.provider_catalog import RESEARCH_MANIFEST
from agent_host.provider_types import (
    EmitProviderEvent,
    ProviderActivityEvidence,
    ProviderEvent,
    ProviderRunRequest,
    ProviderRunResult,
)
from research.engine import ResearchEngine
from research.worker import ResearchWorker


class ResearchAdapter:
    """Native Amadeus research provider; no OpenClaw dependency."""

    provider_id = "research"
    manifest = RESEARCH_MANIFEST

    def __init__(self, engine: ResearchEngine | None = None) -> None:
        self.engine = engine or ResearchEngine()
        self.worker = ResearchWorker(self.engine)
        self._active_runs: set[str] = set()

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> ProviderRunResult:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        forced_mode = str(metadata.get("research_mode") or "").strip().lower()
        mode = forced_mode if forced_mode in {"quick", "deep"} else self.engine.classify(request.task)
        milestones = 0
        execution_items = 0
        self._active_runs.add(run_id)

        async def progress(stage: str, payload: dict) -> None:
            nonlocal milestones, execution_items
            milestones += 1
            if stage in {"searching", "fetching", "analyzing"}:
                execution_items += 1
            await emit(
                ProviderEvent(
                    provider=self.provider_id,
                    run_id=run_id,
                    type="semantic.progress",
                    payload={
                        "category": "diagnostic" if stage in {"planning", "searching", "fetching"} else "validation",
                        "stage": stage,
                        "status": stage.upper(),
                        **payload,
                    },
                    metadata={"research_mode": mode},
                )
            )

        try:
            result = await self.worker.run(request.task, mode=mode, progress=progress)
            if mode == "deep":
                await emit(
                    ProviderEvent(
                        provider=self.provider_id,
                        run_id=run_id,
                        type="artifact.created",
                        payload={
                            "artifact_type": "research.report",
                            "title": f"Research: {request.task[:100]}",
                            "source_count": len(result.evidence),
                            "citation_count": len(result.evidence),
                        },
                        metadata={"research_mode": mode},
                    )
                )
                milestones += 1
            return ProviderRunResult(
                status="done",
                result=result.text,
                metadata={
                    "research_output_mode": mode,
                    "research": {
                        "mode": mode,
                        "search_backend": result.search_backend,
                        "plan_queries": list(result.plan.queries) if result.plan else [],
                        **dict(result.metadata),
                    },
                },
                activity_evidence=ProviderActivityEvidence(
                    terminal_observed=True,
                    progress_milestones=milestones,
                    execution_items=max(1, execution_items),
                ),
            )
        finally:
            self._active_runs.discard(run_id)

    async def cancel(self, run_id: str) -> dict[str, object]:
        # ProviderRuntime owns and cancels the actual asyncio task after this
        # confirmation. Network requests and DeepSeek calls are bounded by their
        # own timeouts, so there is no detached native process to terminate.
        active = str(run_id or "") in self._active_runs
        return {
            "confirmed": True,
            "cancelled": active,
            "reason": "research_run_cancelled" if active else "research_run_not_active",
        }
