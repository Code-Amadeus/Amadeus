"""Exercise the shipping Electron shell without models, credentials, or assets.

The smoke launches the built Electron application. Electron starts the normal
authenticated Python backend, and Playwright connects to Electron's own Chromium
over a loopback CDP port. No chat turn, Provider work, microphone, TTS, model, or
character package is required.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "amadeus.electron-model-less-smoke.v1"


def model_less_backend_environment(
    base: dict[str, str], *, python_executable: str
) -> dict[str, str]:
    env = dict(base)
    env.update(
        {
            "AMADEUS_PYTHON": python_executable,
            "AMADEUS_E2E_NO_TTS": "1",
            "TTS_BACKEND": "disabled",
            "TTS_DEVICE": "cpu",
            "WAKE_ENABLED": "0",
            "VTS_ENABLED": "0",
            "AEC_REALTIME_ENABLED": "0",
            # Exercise the shipping first-run boundary deterministically:
            # B2 remains selected, no remote credential can be inherited from
            # a developer desktop, and the backend must still expose Settings.
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "",
            "OPENAI_API_KEY": "",
            "AUIP_APPSESSION_ROLE_BRANCH_MODE": "b2",
            "AUIP_CONTROL_DECISION_ENABLED": "1",
            "AUIP_ACTION_PROVIDER": "",
            "AUIP_ACTION_MODEL": "",
            "AUIP_ACTION_REASONING_EFFORT": "none",
        }
    )
    return env


async def _wait_enabled(locator: Any, *, timeout: float) -> None:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if await locator.is_visible() and await locator.is_enabled():
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("Chat input did not become enabled after backend connection")


async def _exercise_renderer(page: Any, *, timeout: float) -> dict[str, bool]:
    timeout_ms = max(1_000, int(timeout * 1_000))
    checks: dict[str, bool] = {}

    chat_input = page.locator('textarea[placeholder*="Type a message"]')
    await chat_input.wait_for(state="visible", timeout=timeout_ms)
    await _wait_enabled(chat_input, timeout=timeout)
    checks["chat_connected"] = True

    await page.get_by_role("button", name="Backend", exact=True).click()
    await page.get_by_role("heading", name="Backend", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_text("Connected", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    checks["backend_page_connected"] = True

    await page.get_by_role("button", name="Settings", exact=True).click()
    await page.get_by_role("heading", name="Settings", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_role("button", name="Voice", exact=True).click()
    await page.get_by_text("Speech synthesis", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_role("button", name="General", exact=True).click()
    await page.get_by_text("Optional Runtime Assets", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_text("Visual Runtime Pack", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_text("Kurisu Character Pack", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    checks["settings_and_optional_assets_rendered"] = True

    await page.get_by_role("button", name="Models", exact=True).click()
    await page.get_by_text("Advanced model roles", exact=True).click()
    auip_action_role = page.get_by_text("AUIP action decision", exact=True).last
    await auip_action_role.wait_for(state="visible", timeout=timeout_ms)
    auip_action_card = auip_action_role.locator(
        "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' setting-card ')][1]"
    )
    await auip_action_card.get_by_text("Needs setup", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await auip_action_card.get_by_text(
        re.compile(r"Application actions remain blocked")
    ).wait_for(state="visible", timeout=timeout_ms)
    checks["b2_unavailable_is_visible"] = True

    await page.get_by_role("button", name="Chat", exact=True).click()
    await chat_input.wait_for(state="visible", timeout=timeout_ms)
    rail = page.get_by_role("navigation", name="Chat and artifact navigation")
    await rail.hover()
    await page.get_by_text("PROJECTS", exact=True).first.wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_role("button", name="New Project", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    checks["project_navigation_rendered"] = True

    await page.get_by_role("button", name="Artifacts", exact=True).click()
    await page.get_by_role("navigation", name="Chat and artifact navigation").hover()
    await page.get_by_label("Artifact collections", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_role(
        "button", name=re.compile(r"^Draft artifacts")
    ).click()
    await page.get_by_role("heading", name="Artifacts", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_text("No verified AUIP artifacts yet", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    checks["artifact_navigation_opened"] = True

    viewport = await page.evaluate(
        "() => ({ width: window.innerWidth, height: window.innerHeight })"
    )
    back_to_chat = page.get_by_role("button", name="Back to chat", exact=True)
    await back_to_chat.focus()
    await page.mouse.move(
        max(300, int(viewport["width"]) - 20),
        max(20, int(viewport["height"]) // 2),
    )
    collapse_deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < collapse_deadline:
        if await rail.get_attribute("aria-expanded") == "false":
            break
        await asyncio.sleep(0.1)
    else:
        raise TimeoutError("Chat history rail did not close after pointer exit")

    await back_to_chat.click()
    await chat_input.wait_for(state="visible", timeout=timeout_ms)
    await page.wait_for_timeout(250)

    return checks


async def _wait_clean_exit(
    product: Any,
    *,
    backend_port: int,
    port_is_open: Any,
    timeout: float,
) -> None:
    if product.page is None or product.process is None:
        raise RuntimeError("Electron product did not expose its main window and process")

    process = product.process
    await product.page.close()
    try:
        await asyncio.to_thread(process.wait, max(1.0, timeout))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Electron did not exit after its main window closed") from exc
    if process.returncode != 0:
        raise RuntimeError(f"Electron exited with code {process.returncode}")

    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if not port_is_open(backend_port):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("Electron exited but its owned backend port remained open")


async def run_smoke(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    from tools.e2e_live_product_journey import (
        BACKEND_PORT,
        ElectronProduct,
        WsProbe,
        _free_port,
        _port_is_open,
        code_identity,
    )

    class ModelLessElectronProduct(ElectronProduct):
        """Use the current test interpreter as the explicitly selected backend."""

        def _environment(self) -> dict[str, str]:
            return model_less_backend_environment(
                super()._environment(), python_executable=sys.executable
            )

    started_at = datetime.now(timezone.utc)
    report_root = Path(args.report_dir).resolve()
    run_root = report_root / (
        f"electron_model_less_{started_at.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    )
    run_root.mkdir(parents=True, exist_ok=False)
    report_path = run_root / "report.json"
    product = ModelLessElectronProduct(
        run_root=run_root,
        debug_port=int(args.debug_port or _free_port()),
        no_tts=True,
        identity=code_identity(ROOT),
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "failed",
        "started_at": started_at.isoformat(),
        "paths": {
            "run_root": str(run_root),
            "report": str(report_path),
            "process_log": str(product.log_path),
        },
        "checks": {},
    }

    try:
        await product.start(startup_timeout=float(args.startup_timeout))
        report["checks"]["electron_started_backend"] = True

        async with WsProbe(
            f"ws://127.0.0.1:{BACKEND_PORT}/ws",
            subprotocols=product.backend_websocket_protocols,
        ) as probe:
            runtime = await probe.request("runtime.status", {}, timeout=20.0)
        if not isinstance(runtime.get("server"), dict):
            raise RuntimeError("authenticated runtime.status did not return server state")
        report["checks"]["authenticated_runtime_status"] = True

        if product.page is None:
            raise RuntimeError("Electron renderer is unavailable")
        report["checks"].update(
            await _exercise_renderer(product.page, timeout=float(args.ui_timeout))
        )
        if product.app_page_errors or product.app_console_errors:
            raise RuntimeError(
                "Electron renderer reported errors: "
                + "; ".join(
                    [
                        *product.app_page_errors[:5],
                        *product.app_console_errors[:5],
                    ]
                )
            )

        await _wait_clean_exit(
            product,
            backend_port=BACKEND_PORT,
            port_is_open=_port_is_open,
            timeout=float(args.shutdown_timeout),
        )
        report["checks"]["electron_and_backend_exited"] = True
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["error_traceback"] = traceback.format_exc()
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["app_surface_diagnostics"] = product.app_diagnostics()
        await product.stop()
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return (0 if report["status"] == "passed" else 1), report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir",
        default=str(RUNTIME / "electron_model_less_smoke"),
    )
    parser.add_argument("--debug-port", type=int, default=0)
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--ui-timeout", type=float, default=30.0)
    parser.add_argument("--shutdown-timeout", type=float, default=20.0)
    return parser


def main() -> int:
    # The shared live-product launcher imports normal settings. Pin only this
    # command process before the lazy import in run_smoke; importing this module
    # from pytest remains side-effect free.
    os.environ["AMADEUS_E2E_NO_TTS"] = "1"
    os.environ["TTS_BACKEND"] = "disabled"
    os.environ["TTS_DEVICE"] = "cpu"
    os.environ["WAKE_ENABLED"] = "0"
    exit_code, report = asyncio.run(run_smoke(_parser().parse_args()))
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "report": report.get("paths", {}).get("report"),
                "error": report.get("error", ""),
                "checks": report.get("checks", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
