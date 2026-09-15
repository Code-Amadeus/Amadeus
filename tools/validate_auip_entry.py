"""Boot one completed AUIP HTML entry in an isolated real browser page.

This is an authoring preflight, not a Host attach or gameplay test.  It loads the
entry from disk with its materialized SDK scripts, blocks live transport, and
reports JavaScript errors and stylesheet delivery failures while the page boots.
Layout and keyboard-focus observations are advisory, not acceptance criteria.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.auip_contract import AuipProtocolError, parse_manifest  # noqa: E402


MAX_DIAGNOSTICS = 32
MAX_MESSAGE_CHARS = 2000
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_SETTLE_MILLISECONDS = 300


def _bounded(value: object) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text[:MAX_MESSAGE_CHARS]


def _diagnostic(
    source: str,
    message: object,
    *,
    code: str = "",
    location: object = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "source": source,
        "message": _bounded(message),
    }
    if code:
        item["code"] = code
    if isinstance(location, dict):
        item["location"] = {
            key: value
            for key, value in location.items()
            if key in {"url", "lineNumber", "columnNumber"}
        }
    return item


def _base_result(manifest_path: Path, entry_path: Path) -> dict[str, object]:
    return {
        "ok": False,
        "kind": "app_error",
        "manifest": str(manifest_path),
        "entry": str(entry_path),
        "browser": {
            "engine": "chromium",
            "channel": "msedge" if sys.platform == "win32" else "bundled",
            "freshEphemeralContext": True,
        },
        "transportIsolation": {
            "liveHost": False,
            "externalNetwork": False,
            "isolatedTransportAttempts": [],
            "blockedExternalRequests": [],
        },
        "diagnostics": [],
        "scope": {
            "proves": "entry boot and declared stylesheet delivery completed without captured errors",
            "doesNotProve": (
                "live Host attach, Controller or primary-loop gameplay, action legality, "
                "receipts, or full application acceptance"
            ),
        },
    }


def _required_runtime_assets(manifest: dict[str, object]) -> tuple[str, ...]:
    assets = [
        "sdk/auip-core/managed-v0.js",
        "sdk/auip-web/auip-v0.js",
    ]
    if manifest.get("situationKinds"):
        assets.insert(1, "sdk/auip-core/situations-v0.js")
    if manifest.get("controller"):
        assets.insert(-1, "sdk/auip-core/controller-v0.js")
    return tuple(assets)


def _read_manifest(path: Path) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise AuipProtocolError("manifest_type_invalid", "manifest must be an object")
        parse_manifest(source)
        return source, None
    except (OSError, UnicodeError, json.JSONDecodeError, AuipProtocolError) as exc:
        return None, _diagnostic("manifest", exc, code="manifest_invalid")


async def _inspect_styles(page: Any, diagnostics: list[dict[str, object]]) -> dict[str, object]:
    """Verify declared CSS delivery; visual observations never decide app meaning."""
    observed = await page.evaluate("""() => {
      const links = Array.from(document.querySelectorAll('link[rel~="stylesheet"]'))
        .filter(link => !link.disabled && !link.sheet?.disabled
          && !link.relList.contains('alternate')
          && (!link.media || matchMedia(link.media).matches));
      const base = links.filter(link => new URL(link.href).pathname.endsWith('/amadeus-v1.css'));
      return {
        stylesheets: links.map(link => ({url: link.href, loaded: Boolean(link.sheet)})),
        amadeusBaseReferenced: base.length > 0,
        amadeusBaseApplied: base.length > 0 && Array.from(document.querySelectorAll('.am-app'))
          .some(root => getComputedStyle(root).getPropertyValue('--am-style-version').trim() === '1'),
      };
    }""")
    for sheet in observed["stylesheets"]:
        if not sheet["loaded"] and len(diagnostics) < MAX_DIAGNOSTICS:
            diagnostics.append(_diagnostic("stylesheet", sheet["url"], code="stylesheet_not_loaded"))
    if observed["amadeusBaseReferenced"] and not observed["amadeusBaseApplied"]:
        if len(diagnostics) < MAX_DIAGNOSTICS:
            diagnostics.append(_diagnostic("stylesheet",
                "The referenced Amadeus base is not applied to an .am-app root.",
                code="amadeus_style_not_applied"))
    # A wide canvas or custom focus border may be intentional: report, don't veto.
    observations = []
    for width in (1280, 390):
        await page.set_viewport_size({"width": width, "height": 900})
        metrics = await page.evaluate("""() => ({
          viewportWidth: innerWidth,
          documentWidth: document.documentElement.scrollWidth,
          visibleControls: Array.from(document.querySelectorAll('button,input,select,textarea,[role="button"]'))
            .filter(e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e);
              return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none'; }).length,
        })""")
        observations.append(metrics)
    await page.keyboard.press("Tab")
    focus = await page.evaluate("""() => {
      const e=document.activeElement;
      if (!e || e===document.body || e===document.documentElement) return null;
      const s=getComputedStyle(e);
      return {tag: e.tagName.toLowerCase(), outlineStyle:s.outlineStyle,
        outlineWidth:s.outlineWidth, boxShadow:s.boxShadow};
    }""")
    advisories = [{"code": "document_horizontal_overflow", "viewportWidth": m["viewportWidth"]}
        for m in observations if m["documentWidth"] > m["viewportWidth"] + 1]
    if focus and (focus["outlineStyle"] == "none" or focus["outlineWidth"] == "0px") and focus["boxShadow"] == "none":
        advisories.append({"code": "review_keyboard_focus", "tag": focus["tag"]})
    return {**observed, "viewports": observations, "firstKeyboardFocus": focus,
        "advisories": advisories, "scope": "CSS delivery; visual observations are advisory, not a gameplay or accessibility audit"}


async def validate_entry(
    manifest_path: Path,
    entry_path: Path,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    settle_milliseconds: int = DEFAULT_SETTLE_MILLISECONDS,
) -> dict[str, object]:
    result = _base_result(manifest_path, entry_path)
    manifest, manifest_error = _read_manifest(manifest_path)
    if manifest_error is not None:
        result["diagnostics"] = [manifest_error]
        return result
    if not entry_path.is_file():
        result["diagnostics"] = [
            _diagnostic("entry", entry_path, code="entry_missing")
        ]
        return result

    app_root = manifest_path.parent.resolve()
    required_assets = _required_runtime_assets(manifest or {})
    missing_assets = [
        relative_name
        for relative_name in required_assets
        if not (app_root / Path(relative_name)).is_file()
    ]
    if missing_assets:
        result["kind"] = "host_materialization_error"
        result["diagnostics"] = [
            _diagnostic(
                "runtime_asset",
                (
                    f"Host-materialized runtime asset is missing: {relative_name}. "
                    "Do not edit, copy, or recreate the SDK; report this Host blocker."
                ),
                code="host_runtime_asset_missing",
            )
            for relative_name in missing_assets[:MAX_DIAGNOSTICS]
        ]
        return result

    try:
        from playwright.async_api import (  # type: ignore[import-not-found]
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
            async_playwright,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        result["kind"] = "browser_environment_error"
        result["diagnostics"] = [
            _diagnostic("browser", exc, code="playwright_unavailable")
        ]
        return result

    diagnostics: list[dict[str, object]] = []
    blocked_requests: list[str] = []
    isolated_transport_attempts: list[dict[str, str]] = []
    console_inspections: set[asyncio.Task[None]] = set()
    browser = None
    launched = False
    try:
        async with async_playwright() as playwright:
            launch_options: dict[str, object] = {
                "headless": True,
                "timeout": int(timeout_seconds * 1000),
            }
            if sys.platform == "win32":
                launch_options["channel"] = "msedge"
            browser = await playwright.chromium.launch(**launch_options)
            launched = True
            context = await browser.new_context(
                offline=True,
                service_workers="block",
            )

            async def isolate_external_request(route: Any) -> None:
                url = str(route.request.url)
                scheme = url.split(":", 1)[0].lower()
                if scheme == "file" and route.request.resource_type == "stylesheet":
                    local = Path(url2pathname(urlparse(url).path)).resolve()
                    if not local.is_relative_to(app_root):
                        if len(diagnostics) < MAX_DIAGNOSTICS:
                            diagnostics.append(_diagnostic("stylesheet", url,
                                code="stylesheet_outside_bundle"))
                        await route.abort("blockedbyclient")
                        return
                if scheme in {"file", "data", "blob", "about"}:
                    await route.continue_()
                    return
                if len(blocked_requests) < MAX_DIAGNOSTICS:
                    blocked_requests.append(_bounded(url))
                await route.abort("blockedbyclient")

            await context.route("**/*", isolate_external_request)

            def isolate_websocket(route: Any) -> None:
                if len(isolated_transport_attempts) < MAX_DIAGNOSTICS:
                    isolated_transport_attempts.append(
                        {"kind": "websocket", "url": _bounded(route.url)}
                    )
                route.on_message(lambda _message: None)

            await context.route_web_socket("**", isolate_websocket)
            page = await context.new_page()
            page.set_default_timeout(int(timeout_seconds * 1000))

            def on_console(message: Any) -> None:
                if message.type == "error" and len(diagnostics) < MAX_DIAGNOSTICS:
                    diagnostics.append(
                        _diagnostic(
                            "console",
                            message.text,
                            code="console_error",
                            location=message.location,
                        )
                    )
                elif message.args:
                    task = asyncio.create_task(inspect_caught_console_errors(message))
                    console_inspections.add(task)
                    task.add_done_callback(console_inspections.discard)

            async def inspect_caught_console_errors(message: Any) -> None:
                """Keep official Managed failures observable through app fallback."""

                for argument in message.args:
                    if len(diagnostics) >= MAX_DIAGNOSTICS:
                        return
                    try:
                        caught = await argument.evaluate(
                            """value => {
                              const constructor = globalThis.AmadeusAUIPManaged
                                && globalThis.AmadeusAUIPManaged.ManagedCommitError;
                              if (typeof constructor !== "function"
                                  || !(value instanceof constructor)) return null;
                              return {
                                name: String(value.name || "Error"),
                                code: String(value.code || "managed_commit_error"),
                                detail: String(value.detail || ""),
                                message: String(value.message || value),
                                stack: String(value.stack || ""),
                              };
                            }"""
                        )
                    except Exception:
                        continue
                    if isinstance(caught, dict):
                        details = str(caught.get("stack") or "").strip() or (
                            f"{caught.get('name') or 'Error'}: "
                            f"{caught.get('message') or ''}"
                        )
                        diagnostics.append(
                            _diagnostic(
                                "console",
                                details,
                                code=_bounded(
                                    caught.get("code") or "managed_commit_error"
                                ),
                                location=message.location,
                            )
                        )

            def on_page_error(error: Any) -> None:
                if len(diagnostics) < MAX_DIAGNOSTICS:
                    details = getattr(error, "stack", None) or error
                    diagnostics.append(
                        _diagnostic("pageerror", details, code="javascript_page_error")
                    )

            page.on("console", on_console)
            page.on("pageerror", on_page_error)

            def on_request_failed(request: Any) -> None:
                if request.resource_type == "stylesheet" and len(diagnostics) < MAX_DIAGNOSTICS:
                    diagnostics.append(_diagnostic("stylesheet", request.url,
                        code="stylesheet_load_failed"))

            page.on("requestfailed", on_request_failed)
            await page.goto(
                entry_path.resolve().as_uri(),
                wait_until="load",
                timeout=int(timeout_seconds * 1000),
            )
            await page.wait_for_timeout(settle_milliseconds)
            if console_inspections:
                await asyncio.gather(*tuple(console_inspections))

            observed = await page.evaluate(
                """() => ({
                  scriptSources: Array.from(document.scripts, script => script.src).filter(Boolean),
                  managedApi: typeof globalThis.AmadeusAUIPManaged?.createManagedCore === "function",
                  webApi: typeof globalThis.AmadeusAUIP?.createManagedApp === "function",
                })"""
            )
            result["transportIsolation"]["isolatedTransportAttempts"] = (  # type: ignore[index]
                isolated_transport_attempts
            )
            script_sources = {
                str(value).casefold() for value in observed.get("scriptSources", [])
            }
            for relative_name in required_assets:
                expected = (app_root / Path(relative_name)).resolve().as_uri().casefold()
                if expected not in script_sources and len(diagnostics) < MAX_DIAGNOSTICS:
                    diagnostics.append(
                        _diagnostic(
                            "runtime_asset",
                            relative_name,
                            code="materialized_runtime_asset_not_loaded",
                        )
                    )
            if not observed.get("managedApi") and len(diagnostics) < MAX_DIAGNOSTICS:
                diagnostics.append(
                    _diagnostic(
                        "runtime_asset",
                        "AmadeusAUIPManaged.createManagedCore",
                        code="managed_sdk_unavailable",
                    )
                )
            if not observed.get("webApi") and len(diagnostics) < MAX_DIAGNOSTICS:
                diagnostics.append(
                    _diagnostic(
                        "runtime_asset",
                        "AmadeusAUIP.createManagedApp",
                        code="web_sdk_unavailable",
                    )
                )
            result["styleChecks"] = await _inspect_styles(page, diagnostics)
            await context.close()
    except PlaywrightTimeoutError as exc:
        diagnostics.append(_diagnostic("browser", exc, code="entry_boot_timeout"))
    except PlaywrightError as exc:
        if not launched:
            result["kind"] = "browser_environment_error"
            diagnostics.append(_diagnostic("browser", exc, code="browser_unavailable"))
        else:
            diagnostics.append(_diagnostic("browser", exc, code="entry_boot_failed"))
    except Exception as exc:  # keep unexpected probe failures observable
        result["kind"] = "tool_error"
        diagnostics.append(_diagnostic("preflight", exc, code="preflight_failed"))
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

    result["transportIsolation"]["blockedExternalRequests"] = blocked_requests  # type: ignore[index]
    result["diagnostics"] = diagnostics[:MAX_DIAGNOSTICS]
    result["ok"] = not diagnostics
    if result["ok"]:
        result["kind"] = "ok"
    return result


def _print_text(result: dict[str, object]) -> None:
    if result.get("ok"):
        print(
            "ok: AUIP entry booted in a fresh transport-isolated browser page; "
            "live Host attach and gameplay were not tested"
        )
        for advisory in (result.get("styleChecks") or {}).get("advisories", []):
            print(f"style advisory: {advisory}")
        return
    print(f"AUIP entry preflight failed ({result.get('kind')}):", file=sys.stderr)
    for item in result.get("diagnostics", []):
        if isinstance(item, dict):
            print(
                f"- {item.get('source')} [{item.get('code')}]: {item.get('message')}",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("entry", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--settle-ms", type=int, default=DEFAULT_SETTLE_MILLISECONDS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 1.0 <= args.timeout_seconds <= 60.0:
        parser.error("--timeout-seconds must be between 1 and 60")
    if not 0 <= args.settle_ms <= 5000:
        parser.error("--settle-ms must be between 0 and 5000")

    manifest = args.manifest.resolve()
    entry = args.entry.resolve()
    result = asyncio.run(
        validate_entry(
            manifest,
            entry,
            timeout_seconds=args.timeout_seconds,
            settle_milliseconds=args.settle_ms,
        )
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_text(result)
    if result.get("ok"):
        return 0
    return {
        "app_error": 2,
        "browser_environment_error": 3,
        "host_materialization_error": 4,
        "tool_error": 5,
    }.get(str(result.get("kind")), 5)


if __name__ == "__main__":
    raise SystemExit(main())
