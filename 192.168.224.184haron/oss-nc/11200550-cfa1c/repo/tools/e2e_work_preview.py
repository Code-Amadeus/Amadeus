"""Run one real Electron Work Preview hot-reload journey.

The journey uses an isolated ledger and workspace, but launches the shipping
Electron main process and Python backend.  It proves that a Host-authored
WorkItem preview opens as a separate shell, renders in the sandboxed content
surface, reloads after a filesystem change, and releases its Host session when
the person closes the window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.work_ledger_store import WorkLedgerStore
from server.work_ledger_coordinator import WorkLedgerCoordinator
from tools.e2e_live_product_journey import (
    BACKEND_PORT,
    ElectronProduct,
    WsProbe,
    _free_port,
)
from tools.semantic_journey_evidence import code_identity

RUNTIME = ROOT / "runtime" / "work_preview_journeys"


def _seed(run_root: Path) -> tuple[str, str, Path]:
    state = run_root / "state"
    workspace = state / "scratch" / "preview-site"
    workspace.mkdir(parents=True, exist_ok=True)
    ledger_path = state / "work_ledger.sqlite3"
    with WorkLedgerStore(ledger_path) as store:
        project = store.create_or_get_project(workspace, name="Preview site")
        item = store.create_work_item(
            project.project_id,
            title="Live preview page",
            workspace_path=workspace,
        )
        attempt = store.create_attempt(
            item.work_item_id,
            provider="codex",
            task="Build a previewable web page",
        )
        # Startup reconciliation correctly retires active Attempts that have
        # no live Provider runtime. Seed a terminal history row, then create
        # the journey's active Attempt after the Backend is ready.
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        coordinator = WorkLedgerCoordinator(store)
        coordinator.select(item.work_item_id)
        return item.work_item_id, attempt.attempt_id, workspace


async def _find_page(product: ElectronProduct, marker: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for page in await product.app_pages():
            if marker in str(page.url):
                return page
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Electron page did not appear: {marker}")


async def _wait_text(product: ElectronProduct, expected: str, timeout: float = 12.0):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        for page in await product.app_pages():
            if not str(page.url).startswith("http://127.0.0.1:"):
                continue
            try:
                last = await page.locator("#version").inner_text(timeout=500)
            except Exception:
                continue
            if last == expected:
                return page
        await asyncio.sleep(0.1)
    raise AssertionError(f"preview text did not become {expected!r}; observed {last!r}")


async def run() -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = RUNTIME / f"work_preview_{stamp}"
    run_root.mkdir(parents=True, exist_ok=False)
    work_item_id, attempt_id, workspace = _seed(run_root)
    identity = code_identity(ROOT)
    product = ElectronProduct(
        run_root=run_root,
        debug_port=_free_port(),
        no_tts=True,
        identity=identity,
    )
    report: dict[str, Any] = {
        "schema": "amadeus.work-preview-journey.v1",
        "run_root": str(run_root),
        "work_item_id": work_item_id,
        "attempt_id": attempt_id,
        "checks": {},
    }
    try:
        await product.start(startup_timeout=120.0)
        ledger_path = run_root / "state" / "work_ledger.sqlite3"
        with WorkLedgerStore(ledger_path) as store:
            live_attempt = store.create_attempt(
                work_item_id,
                provider="e2e",
                task="Exercise Work Preview live lifecycle",
            )
            store.update_attempt(live_attempt.attempt_id, execution_status="running")
        attempt_id = live_attempt.attempt_id
        report["attempt_id"] = attempt_id
        async with WsProbe(
            f"ws://127.0.0.1:{BACKEND_PORT}/ws",
            subprotocols=product.backend_websocket_protocols,
        ) as probe:
            listing = await probe.request("work.list", {})
            work = listing.get("work") if isinstance(listing.get("work"), dict) else {}
            assert work.get("selectedWorkItemId") == work_item_id
            opened = await probe.request(
                "work.preview.open",
                {
                    "work_item_id": work_item_id,
                    "attempt_id": attempt_id,
                    "revision": work.get("revision"),
                },
            )
            preview = opened.get("preview") if isinstance(opened.get("preview"), dict) else {}
            assert opened.get("ok") is True
            assert preview.get("status") == "waiting"
            assert preview.get("url") == ""
            assert preview.get("lifecycle") == "live"
            preview_id = str(preview.get("previewId") or "")
            assert preview_id

            shell = await _find_page(product, "previewWindow=1")
            await shell.locator(".work-preview-title-copy strong").wait_for(
                state="visible",
                timeout=10_000,
            )
            assert await shell.locator(".work-preview-load-label").text_content() == "WAITING"
            report["checks"]["waiting_shell_opened"] = True

            discovery_start = len(probe.state.events)
            (workspace / "index.html").write_text(
                """<!doctype html>
<html><head><meta charset="utf-8"><title>Preview Page</title></head>
<body><main id="version">VERSION ONE</main></body></html>
""",
                encoding="utf-8",
            )
            await probe.wait_event(
                lambda event: (
                    event.method == "work.preview.updated"
                    and str((event.params.get("preview") or {}).get("previewId") or "")
                    == preview_id
                    and str((event.params.get("preview") or {}).get("status") or "")
                    == "ready"
                ),
                timeout=12.0,
                after=discovery_start,
                description="preview first entry discovery",
            )
            await _wait_text(product, "VERSION ONE")
            await shell.wait_for_function(
                "document.querySelector('.work-preview-load-label')?.textContent === 'LIVE'",
                timeout=10_000,
            )
            screenshots = run_root / "screenshots"
            screenshots.mkdir(parents=True, exist_ok=True)
            await shell.screenshot(path=str(screenshots / "preview-shell.png"))
            report["checks"]["first_entry_bound_without_reopen"] = True
            report["checks"]["initial_page_rendered"] = True

            event_start = len(probe.state.events)
            (workspace / "index.html").write_text(
                """<!doctype html>
<html><head><meta charset="utf-8"><title>Preview Page</title></head>
<body><main id="version">VERSION TWO</main></body></html>
""",
                encoding="utf-8",
            )
            await probe.wait_event(
                lambda event: (
                    event.method == "work.preview.updated"
                    and str((event.params.get("preview") or {}).get("previewId") or "")
                    == preview_id
                    and str(event.params.get("reason") or "") == "content_changed"
                ),
                timeout=12.0,
                after=event_start,
                description="preview content-change generation",
            )
            await _wait_text(product, "VERSION TWO")
            report["checks"]["filesystem_change_published"] = True
            report["checks"]["same_surface_hot_reloaded"] = True

            holding_start = len(probe.state.events)
            with WorkLedgerStore(ledger_path) as store:
                store.update_attempt(attempt_id, execution_status="succeeded")
            holding_event = await probe.wait_event(
                lambda event: (
                    event.method == "work.preview.updated"
                    and str((event.params.get("preview") or {}).get("previewId") or "")
                    == preview_id
                    and str((event.params.get("preview") or {}).get("lifecycle") or "")
                    == "holding"
                ),
                timeout=12.0,
                after=holding_start,
                description="preview terminal holding lifecycle",
            )
            holding_preview = holding_event.params.get("preview") or {}
            holding_content_revision = int(holding_preview.get("contentRevision") or 0)
            (workspace / "index.html").write_text(
                """<!doctype html>
<html><head><meta charset="utf-8"><title>Preview Page</title></head>
<body><main id="version">VERSION THREE</main></body></html>
""",
                encoding="utf-8",
            )
            await asyncio.sleep(1.5)
            held = await probe.request(
                "work.preview.get",
                {"work_item_id": work_item_id},
            )
            held_preview = held.get("preview") or {}
            assert held_preview.get("lifecycle") == "holding"
            assert int(held_preview.get("contentRevision") or 0) == holding_content_revision
            await _wait_text(product, "VERSION TWO", timeout=2.0)
            report["checks"]["terminal_holds_last_render"] = True

            await shell.locator('button[aria-label="Close preview"]').click()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not shell.is_closed():
                await asyncio.sleep(0.1)
            if not shell.is_closed():
                notice = await shell.locator(".work-preview-notice").text_content()
                host_state = await probe.request(
                    "work.preview.get",
                    {"work_item_id": work_item_id},
                )
                raise AssertionError(
                    "Preview shell remained open after close: "
                    f"notice={notice!r} host={host_state!r}"
                )
            closed = await probe.request(
                "work.preview.get",
                {"work_item_id": work_item_id},
            )
            assert (closed.get("preview") or {}).get("status") == "closed"
            report["checks"]["close_reclaimed_host_session"] = True
            report["events"] = [event.to_dict() for event in probe.state.events]
            report["app_diagnostics"] = product.app_diagnostics()
    finally:
        await product.stop()

    report["passed"] = all(report["checks"].values()) and not any(
        report.get("app_diagnostics", {}).values()
    )
    report_path = run_root / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    report = asyncio.run(run())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
