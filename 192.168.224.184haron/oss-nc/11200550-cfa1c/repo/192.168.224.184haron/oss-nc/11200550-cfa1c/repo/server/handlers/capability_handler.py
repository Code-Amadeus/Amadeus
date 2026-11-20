"""Read-only client surface for the Host capability catalog."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from server.capability_catalog import CapabilityCatalog, CapabilityPackage
from server.protocol import Method
from server.ws_handler import RequestHandler


logger = logging.getLogger(__name__)


class CapabilityHandler(RequestHandler):
    methods = [Method.CAPABILITY_LIST]

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        extra_packages: Callable[[], Iterable[CapabilityPackage]] | None = None,
    ) -> None:
        self.catalog = catalog
        self.extra_packages = extra_packages

    async def handle(self, method: str, params: dict) -> dict | None:
        if method != Method.CAPABILITY_LIST:
            return None
        data = params if isinstance(params, dict) else {}
        dynamic: tuple[CapabilityPackage, ...] = ()
        projection_errors: list[dict[str, str]] = []
        if self.extra_packages is not None:
            try:
                dynamic = tuple(self.extra_packages())
            except Exception:
                # A broken optional projection must not hide built-in
                # Providers or Skills, and its raw diagnostic may contain a
                # local path.  Preserve a bounded typed health signal only.
                logger.warning("dynamic capability projection failed")
                projection_errors.append(
                    {
                        "source": "dynamic",
                        "code": "projection_failed",
                    }
                )
        snapshot = self.catalog.snapshot(
            kind=str(data.get("kind") or ""),
            surface=str(data.get("surface") or ""),
            include_disabled=bool(data.get("include_disabled", False)),
            extra_packages=dynamic,
        )
        if projection_errors:
            snapshot["projection_errors"] = projection_errors
        return snapshot
