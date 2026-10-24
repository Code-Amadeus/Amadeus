"""Deliberate failures in the shipping app, using only an owned disposable profile."""

from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def prepare_startup_failure(base, reference):
    from tools.probes.extension_theme_lifecycle import build_wheel
    from tools.extension_package import host_profile
    from server.extensions.catalog import policy
    from config.subprocess_environment import isolated_child_environment

    source = base / "failing-tone-source"
    shutil.copytree(
        ROOT / "examples/extensions/tone-tts",
        source,
        ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"),
    )
    package = source / "src/amadeus_example_tone"
    manifest_path = package / "amadeus-extension.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "0.3.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    project = source / "pyproject.toml"
    project.write_text(
        project.read_text().replace('version = "0.1.0"', 'version = "0.3.0"'), encoding="utf-8"
    )
    (package / "__init__.py").write_text(
        'raise RuntimeError("synthetic startup rejection")\n', encoding="utf-8"
    )
    destination = base / "failing-wheel"
    destination.mkdir()
    build_wheel(source, destination, isolated_child_environment())
    wheel = next(destination.glob("*.whl"))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    facts = {
        "wheel": {"url": "https://artifact.test/" + wheel.name, "sha256": digest},
        "publisher": reference["publisher"],
        "review": {**reference["review"], "sha256": digest},
        "withdrawal": None,
    }
    entry = policy().inspect_entry(facts, wheel, ROOT, Path(sys.executable), host_profile())
    return entry, wheel


async def exercise_failures(product, fixture):
    from tools.probes.extension_release_shell import evaluate_main
    from tools.probes.extension_startup import release_fixture_script
    from tools.smoke_electron_model_less import _wait_enabled

    checks = []
    report = {
        "ok": False,
        "checks": checks,
        "fixture_authority": True,
        "native_confirmation": "automated",
        "damage": "NUL file injection while app is stopped; not physical power loss",
    }
    data = json.loads((fixture / "fixture.json").read_text())
    index = copy.deepcopy(data["index"])
    index["sequence"] = 3
    state_path = product.run_root / "state/electron-user-data/settings.json"
    assert state_path.resolve().is_relative_to(product.run_root.resolve())
    theme = "com.example.desktop-theme"
    tone = "com.example.tone-tts"

    def check(name, value=True):
        if not value:
            raise AssertionError(name)
        checks.append(name)
        (product.run_root / "extension-failure-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )

    async def main(expression):
        return await evaluate_main(product, expression)

    async def catalog(request):
        return await asyncio.wait_for(
            product.page.evaluate("r=>window.amadeus.extensionCatalog(r)", request), 180
        )

    async def update(package_id, enabled):
        response = await product.page.evaluate(
            "r=>window.amadeus.updateExtension(r)", {"id": package_id, "enabled": enabled}
        )
        assert response["ok"], response.get("error")

    async def refresh():
        await main("extensionReleaseFixture.index=" + json.dumps(index))
        return await catalog({"action": "refresh"})

    async def install(package_id, version):
        previous = json.loads(state_path.read_text())['extensions'].get(package_id)
        response = await catalog({"action": "schedule", "id": package_id, "version": version})
        if not response['ok']:
            return response
        assert response.get('scheduled')
        assert json.loads(state_path.read_text())['extensions'].get(package_id) == previous
        await restart()
        current = json.loads(state_path.read_text())['extensions'].get(package_id)
        snapshot = await product.page.evaluate('window.amadeus.getDesktopSettings()')
        return {'ok': bool(current and current['version'] == version), 'error': '; '.join(snapshot['extensionErrors'])}

    async def restart(damage=None, offline=False):
        old_pid = product.process.pid
        behavior = await main('(()=>{const s=extensionReleaseFixture;return {badWheel:s.badWheel,confirmations:s.confirmations}})()')
        await main("(()=>{setImmediate(()=>process.getBuiltinModule('module').createRequire(process.execPath)('electron').app.quit());return true})()")
        await asyncio.to_thread(product.process.wait, 30)
        await product.stop()
        assert product.process.poll() is not None
        phase = report.get("app_restarts", 0) + 1
        report["app_restarts"] = phase
        shutil.copyfile(product.log_path, product.run_root / f"restart-{phase}-previous.log")
        if damage:
            for path in damage:
                assert path.resolve().is_relative_to(product.run_root.resolve())
                path.write_bytes(b"\0" * 1536)
        await product.start(startup_timeout=180, main_startup_script=release_fixture_script(
            fixture, {**behavior, 'index': index, 'offline': offline}))
        await _wait_enabled(product.page.locator('textarea[placeholder*="Type a message"]'), timeout=60)
        assert product.process.pid != old_pid

    async def backend_restart():
        assert await product.page.evaluate("window.amadeus.restartBackend()")

    async def runtime_state(version, expected):
        for _ in range(80):
            runtime = await product.runtime_status()
            rows = runtime.get("extensions", [])
            assert isinstance(rows, list), "Runtime extension status is unavailable"
            found = next((r for r in rows if r["id"] == tone and r["version"] == version), None)
            if found and found["state"] in expected:
                return found
            await asyncio.sleep(0.5)
        report["unexpected_runtime"] = rows
        (product.run_root / "extension-failure-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        raise AssertionError("Runtime never reached expected plugin state")

    try:
        await restart()
        check(
            "fresh app process completes prior pending removals",
            not json.loads(state_path.read_text())["extensions"],
        )
        selected = await catalog({'action':'schedule', 'id':theme, 'version':'0.1.0'})
        assert selected['ok'] and selected['scheduled']
        cancelled = await catalog({'action':'cancel-scheduled'})
        check(
            "cancelled scheduled install neither downloads nor installs",
            cancelled['ok'] and not cancelled['catalog'].get('scheduledInstall')
            and not json.loads(state_path.read_text())["extensions"]
            and not any('.whl' in path for path in await main('extensionReleaseFixture.requests')),
        )
        denied = await catalog({'action':'install', 'id':theme, 'version':'0.1.0'})
        check('live backend refuses immediate installation', not denied['ok'] and 'installation is blocked' in denied['error'])
        assert (await install(theme, "0.1.0"))["ok"]
        await update(theme, True)
        await product.page.evaluate(
            "window.amadeus.updateDesktopSettings({values:{AMADEUS_UI_THEME:'com.example.desktop-theme/day'}})"
        )
        await main("extensionReleaseFixture.badWheel=true")
        rejected = await install(theme, "0.2.0")
        check(
            "wrong digest update preserves previous installed and applied version",
            not rejected["ok"]
            and json.loads(state_path.read_text())["extensions"][theme]["version"] == "0.1.0"
            and (await product.page.evaluate("window.amadeus.getDesktopTheme()"))["theme"][
                "version"
            ]
            == "0.1.0",
        )
        await main("extensionReleaseFixture.badWheel=false")
        index["sequence"] += 1
        next(e for e in index["entries"] if e["id"] == theme and e["version"] == "0.2.0")[
            "withdrawal"
        ] = {"reason": "copyright", "evidence": "https://fixture.test/copyright"}
        assert (await refresh())["ok"]
        check(
            "copyright withdrawal blocks distribution without security ban",
            not (await install(theme, "0.2.0"))["ok"]
            and not json.loads(state_path.read_text())["extensionSecurity"]["decisions"],
        )
        theme_data = (
            state_path.parent / "extension-data" / hashlib.sha256(theme.encode()).hexdigest()
        )
        (theme_data / "retained.txt").write_text("synthetic retained data")
        assert (await product.page.evaluate("id=>window.amadeus.removeExtension(id)", theme))["ok"]
        check("default uninstall retains data", (theme_data / "retained.txt").is_file())
        assert (await install(theme, "0.1.0"))["ok"]
        assert (
            await product.page.evaluate(
                "id=>window.amadeus.removeExtension(id,{deleteData:true})", theme
            )
        )["ok"]
        check("explicit uninstall deletes owned data", not theme_data.exists())
        assert (await install(tone, "0.1.0"))["ok"]
        await update(tone, True)
        await backend_restart()
        check(
            "real backend activates installed process package",
            bool(await runtime_state("0.1.0", {"active"})),
        )
        index["sequence"] += 1
        index["entries"].append(data["startup_failure"])
        assert (await refresh())["ok"]
        assert (await install(tone, "0.3.0"))["ok"]
        check(
            "updated package startup failure is visible without automatic rollback",
            bool(await runtime_state("0.3.0", {"error"}))
            and json.loads(state_path.read_text())["extensions"][tone]["version"] == "0.3.0",
        )
        health = await catalog({'action':'health'})
        check('failed update cannot be kept', health['catalog']['pendingUpdate']['recoveryRequired']
              and not (await catalog({'action':'confirm-update'}))['ok'])
        assert (await catalog({'action':'rollback'}))['scheduled']
        await restart()
        check('explicit rollback restores the previous verified runtime',
              json.loads(state_path.read_text())['extensions'][tone]['version'] == '0.1.0'
              and bool(await runtime_state('0.1.0', {'active'}))
              and not (await catalog({'action':'get'}))['catalog'].get('pendingUpdate'))
        await update(tone, False)
        assert (await product.page.evaluate("id=>window.amadeus.removeExtension(id)", tone))["ok"]
        await backend_restart()
        assert (await install(theme, "0.1.0"))["ok"]
        await update(theme, True)
        await product.page.evaluate(
            "window.amadeus.updateDesktopSettings({values:{AMADEUS_UI_THEME:'com.example.desktop-theme/day'}})"
        )
        target = next(e for e in index["entries"] if e["id"] == theme and e["version"] == "0.1.0")
        index["sequence"] += 1
        index["security"] = [
            {
                "id": theme,
                "version": "0.1.0",
                "sha256": target["sha256"],
                "status": "blocked",
                "sequence": index["sequence"],
                "reason": "security",
                "evidence": "https://fixture.test/security",
            }
        ]
        assert (await refresh())["ok"]
        check(
            "ban suppresses active theme immediately",
            not (await product.page.evaluate("window.amadeus.getDesktopTheme()")).get("theme"),
        )
        cache = state_path.parent / "extension-catalog/index.json"
        cache.unlink()
        await restart([state_path], offline=True)
        observed = await catalog({"action": "get"})
        check(
            "fresh process restores primary NUL damage from backup without cache/network",
            observed["catalog"]["installed"][0]["blocked"]
            and json.loads(state_path.with_suffix(".json.bak").read_text())["extensionSecurity"][
                "status"
            ]
            == "ready",
        )
        # The same blocked artifact through the existing local picker boundary.
        wheel = data["wheels"][Path(target["url"]).name]
        await main(
            "(()=>{const e=process.getBuiltinModule('module').createRequire(process.execPath)('electron');e.dialog.showOpenDialog=async()=>({canceled:false,filePaths:["
            + json.dumps(wheel)
            + "]});return true})()"
        )
        local = await product.page.evaluate("window.amadeus.installExtension()")
        assert local['ok'] and local['scheduled']
        prior = json.loads(state_path.read_text())['extensions'][theme]['installId']
        await restart(offline=True)
        observed = await product.page.evaluate('window.amadeus.getDesktopSettings()')
        check(
            "offline known ban blocks scheduled local wheel at startup",
            json.loads(state_path.read_text())['extensions'][theme]['installId'] == prior
            and any('blocked' in error for error in observed['extensionErrors']),
        )
        await main("extensionReleaseFixture.offline=false")
        rollback = copy.deepcopy(index)
        rollback["sequence"] -= 1
        rollback["security"] = []
        await main("extensionReleaseFixture.index=" + json.dumps(rollback))
        check(
            "lower sequence cannot erase a recovered ban",
            not (await catalog({"action": "refresh"}))["ok"],
        )
        await restart([state_path, state_path.with_suffix(".json.bak")], offline=True)
        fault = json.loads(state_path.read_text())["extensionSecurity"]
        check(
            "both NUL copies persist unavailable safety across fresh process",
            fault["status"] == "unavailable",
        )
        assert (
            await product.page.evaluate(
                "window.amadeus.updateDesktopSettings({values:{AMADEUS_UI_LOCALE:'en-US'}})"
            )
        )["ok"]
        check(
            "ordinary settings save cannot clear damaged safety",
            json.loads(state_path.read_text())["extensionSecurity"]["status"] == "unavailable",
        )
        await main("extensionReleaseFixture.offline=false")
        check(
            "normal refresh cannot implicitly recover damaged safety",
            not (await catalog({"action": "refresh"}))["ok"],
        )
        recovered = await catalog({"action": "recover"})
        check(
            "explicit official recovery restores safety but not lost inventory",
            recovered["ok"]
            and not recovered["catalog"]["installed"]
            and json.loads(state_path.read_text())["extensionSecurity"]["decisions"][0]["blocked"],
        )
        check(
            "recovered ban still rejects catalog artifact",
            not (await install(theme, "0.1.0"))["ok"],
        )
        check(
            "failed transactions leave no download staging",
            not list((state_path.parent / "extension-catalog/staging").iterdir()),
        )
        await product.screenshot("release-failure-recovered")
        report["ok"] = True
        report["final_catalog_sequence"] = index["sequence"]
        report["startup_failure_artifact"] = {
            k: data["startup_failure"][k] for k in ("id", "version", "sha256")
        }
        return report
    finally:
        (product.run_root / "extension-failure-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
