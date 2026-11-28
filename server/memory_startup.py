"""Trusted Python-only desktop composition of existing native maintenance.

No environment loader or protocol entry point constructs these options.
The caller supplies authority, providers, clock and the original SDK budget.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class DesktopMemoryMaintenance:
    formation_for_session: Callable[[str], bool]
    access: Any
    warm_provider: Any
    clock: Callable[[], int]
    window_id: str
    budget: Any
    cold_provider: Any = None
    config: Any = None
    schedule_path: Any = None
    context: Any = None
    observer: Any = None
    reformation_enabled: bool = False
    warm_adoption_selector: Callable | None = None
    cold_adoption_selector: Callable | None = None
    automatic_cold_context: bool = False
    cold_generation_enabled: bool = True

    def __post_init__(self):
        if not callable(self.formation_for_session):
            raise ValueError("formation_for_session_callback_required")
        if type(self.reformation_enabled) is not bool:
            raise ValueError("invalid_reformation_opt_in")

    async def bind(self, host, *, resource_ready):
        # Join the normal Host startup; it remains the sole Store/source owner.
        if host._startup is not None:
            await asyncio.shield(host._startup)
        return await host.bind_maintenance(enabled=True,
            access=self.access, warm_provider=self.warm_provider,
            cold_provider=self.cold_provider, clock=self.clock,
            window_id=self.window_id, budget=self.budget, config=self.config,
            schedule_path=self.schedule_path, context=self.context,
            observer=self.observer, resource_ready=resource_ready,
            reformation_enabled=self.reformation_enabled,
            warm_adoption_selector=self.warm_adoption_selector,
            cold_adoption_selector=self.cold_adoption_selector,
            automatic_cold_context=self.automatic_cold_context,
            cold_generation_enabled=self.cold_generation_enabled)
