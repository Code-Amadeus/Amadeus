"""Observe real native confirmations; operator clicks are never replaced by a stub."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


async def exercise_native(product, fixture: Path):
    from tools.probes.extension_release_shell import evaluate_main

    report = {"ok": False, "operator": "external UI operator (record separately)",
              "fixture_authority": True, "native_confirmation": "real dialog, no automatic response",
              "checks": []}
    destination = product.run_root / "native-confirmation-report.json"

    def stage(name):
        report["stage"] = name
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("Native confirmation: " + name, flush=True)

    async def catalog(request):
        return await asyncio.wait_for(
            product.page.evaluate("r=>window.amadeus.extensionCatalog(r)", request), 300)

    def inventory():
        return json.loads((product.run_root / "state/electron-user-data/settings.json").read_text())["extensions"]

    try:
        code = (ROOT / "electron/tests/fixtures/extensionRelease.cjs").read_text()
        await evaluate_main(product, f"(async()=>{{{code};return setupReleaseFixture({json.dumps(str(fixture))},false)}})()")
        assert (await catalog({"action": "refresh"}))["ok"]
        request = {"action": "install", "id": "com.example.desktop-theme", "version": "0.1.0"}
        stage("cancel installation in the real native dialog")
        result = await catalog(request)
        assert result.get("cancelled") and not inventory(), result
        report["checks"].append("native Cancel leaves no installed package")
        stage("confirm installation in the real native dialog")
        result = await catalog(request)
        assert result["ok"] and inventory()[request["id"]]["enabled"] is False, result
        report["checks"].append("native Install creates a disabled package")
        stage("confirm activation in the real native dialog")
        result = await asyncio.wait_for(product.page.evaluate(
            "id=>window.amadeus.updateExtension({id,enabled:true})", request["id"]), 300)
        assert result["ok"] and inventory()[request["id"]]["enabled"] is True, result
        report["checks"].append("native activation confirmation enables the package")
        result = await product.page.evaluate("window.amadeus.updateDesktopSettings({values:{AMADEUS_UI_THEME:'com.example.desktop-theme/day'}})")
        assert result["ok"]
        theme = await product.page.evaluate("window.amadeus.getDesktopTheme()")
        assert theme["theme"]["version"] == "0.1.0", theme
        report["checks"].append("confirmed theme becomes active")
        observed = await evaluate_main(product, "({count:extensionReleaseFixture.confirmations,last:extensionReleaseFixture.lastConfirmation})")
        assert observed["count"] == 3, observed
        report["observed_dialogs"] = observed
        report["ok"] = True
        stage("complete")
        return report
    finally:
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
