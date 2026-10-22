"""Exercise extension IPC/UI in the shipping shell; synthetic authority and automated dialogs.

Only called by the explicit model-less --extension-fixture smoke. The loopback
inspector belongs to its isolated no-credentials process; production has no test API.
"""

from __future__ import annotations
import asyncio
import base64
import csv
import io
import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


async def evaluate_main(product, expression):
    import websockets

    def address():
        connection = http.client.HTTPConnection("127.0.0.1", product.main_debug_port, timeout=3)
        try:
            connection.request("GET", "/json/list")
            targets = json.loads(connection.getresponse().read())
            assert len(targets) == 1
            url = targets[0]["webSocketDebuggerUrl"]
            assert url.startswith(f"ws://127.0.0.1:{product.main_debug_port}/")
            return url
        finally:
            connection.close()

    guarded = f"(async()=>{{if(process.pid!=={product.process.pid})throw Error('Unexpected process');return await ({expression})}})()"
    async with websockets.connect(await asyncio.to_thread(address), open_timeout=3) as socket:
        await socket.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": guarded, "awaitPromise": True, "returnByValue": True},
                }
            )
        )
        while True:
            response = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
            if response.get("id") == 1:
                result = response["result"]
                if result.get("exceptionDetails"):
                    raise RuntimeError(str(result["exceptionDetails"]))
                return result["result"].get("value")


def verify_checker_schemas(checker_root: Path, wheel: Path, python: str = sys.executable):
    """Run the shipped helper against a real v0 wheel and its v1 icon variant."""
    helper = checker_root / "tools/extension_package.py"
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest_name, = [name for name in members if name.endswith("/amadeus-extension.json")]
    manifest = json.loads(members[manifest_name])
    assert manifest["schema"] == "amadeus.extension/v0"
    results = {}
    with tempfile.TemporaryDirectory(prefix="extension-bundle-schemas-") as temporary:
        target = Path(temporary) / wheel.name
        for schema in ("amadeus.extension/v0", "amadeus.extension/v1"):
            source = wheel
            if schema.endswith("/v1"):
                manifest.update(schema=schema, icon="qualification-icon.png")
                members[manifest_name] = json.dumps(manifest).encode("utf-8")
                members[manifest_name.rsplit("/", 1)[0] + "/qualification-icon.png"] = (
                    ROOT / "electron/tests/fixtures/extension-preview.png"
                ).read_bytes()
                record = next((name for name in members if name.endswith(".dist-info/RECORD")), None)
                if record:
                    rows = io.StringIO(newline="")
                    writer = csv.writer(rows, lineterminator="\n")
                    for name, content in sorted(members.items()):
                        if name != record:
                            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                            writer.writerow((name, "sha256=" + digest, len(content)))
                    writer.writerow((record, "", ""))
                    members[record] = rows.getvalue().encode()
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, content in members.items():
                        archive.writestr(name, content)
                source = target
            process = subprocess.run(
                [python, "-I", str(helper), "check-wheel", str(source)],
                cwd=temporary, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            assert process.returncode == 0, process.stdout + process.stderr
            result = json.loads(process.stdout)
            assert result["ok"] and result["manifest"]["schema"] == schema
            results[schema] = {"ok": True, "digest": result["digest"]}
    return results


async def exercise(product, fixture: Path):
    from playwright.async_api import expect
    from config.process_tree import ProcessTree

    page = product.page
    report = {
        "checks": [],
        "fixture_authority": True,
        "native_confirmation": "automated",
        "backend": "normal authenticated model-less runtime",
        "fixture_artifacts": [],
        "used_artifacts": [],
    }

    def check(name, value=True):
        if not value:
            raise AssertionError(name)
        report["checks"].append(name)

    async def catalog(request):
        return await page.evaluate("request=>window.amadeus.extensionCatalog(request)", request)

    async def button(name):
        await (
            page.get_by_role("button", name=name, exact=True)
            .and_(page.locator("button:enabled"))
            .click(timeout=30000)
        )

    async def configure(enabled, provider=None):
        if provider:
            await button("Configure extension")
            await (page.locator(".settings-form-provider-choices label")
                   .filter(has_text=provider).locator("input").check(timeout=30000))
            await button("Save extension")
            await expect(page.locator('form[aria-label="Configure extension"]')).to_have_count(0, timeout=30000)
        toggle = page.locator('#extension-panel-installed [role="switch"][id^="extension-enabled-"]')
        if (await toggle.get_attribute("aria-checked") == "true") != enabled:
            await toggle.click()
        await expect(toggle).to_have_attribute("aria-checked", str(enabled).lower(), timeout=30000)

    async def install(package_id, version):
        await page.get_by_role("tab", name="Browse", exact=True).click()
        await page.get_by_role("searchbox").fill(package_id)
        await button("View plugin details")
        selection = page.locator(".extension-catalog-detail select")
        if await selection.count():
            await selection.select_option(version)
        await button("Install plugin…")
        await expect(page.locator("#extension-panel-installed")).to_be_visible(timeout=120000)
        await expect(page.locator(f'[data-extension-id="{package_id}"][data-extension-version="{version}"]')).to_be_visible(
            timeout=120000
        )
        await expect(page.locator(".extension-download-progress")).to_have_count(0, timeout=120000)

    resource_root = (
        product.packaged_executable.parent / "resources" if product.packaged_executable else ROOT
    )
    checker_root = resource_root / "extension-host" if product.packaged_executable else ROOT
    sdk_directory = (
        resource_root / "extension-sdk"
        if product.packaged_executable
        else ROOT / "vendor/extension-sdk"
    )
    report["protocol_pin"] = json.loads(
        (checker_root / "server/extensions/protocol-source.json").read_text()
    )
    report["catalog_pin"] = json.loads(
        (checker_root / "server/extensions/catalog-source.json").read_text()
    )
    if product.packaged_executable:
        fixture_data = json.loads((fixture / "fixture.json").read_text(encoding="utf-8"))
        entry = next(e for e in fixture_data["index"]["entries"] if e["id"] == "com.example.desktop-theme" and e["version"] == "0.1.0")
        from urllib.parse import urlsplit
        wheel = Path(fixture_data["wheels"][Path(urlsplit(entry["url"]).path).name])
        report["packaged_schema_checks"] = await asyncio.to_thread(verify_checker_schemas, checker_root, wheel)
        check("packaged helper accepts both v0 and v1 icon wheels")
        report["checker_files_sha256"] = {
            p.relative_to(checker_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(checker_root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        }
    report["sdk_sha256"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sdk_directory.glob("*.whl")
    }
    if product.packaged_executable:
        report["build_sha256"] = {
            "executable": hashlib.sha256(product.packaged_executable.read_bytes()).hexdigest(),
            "app.asar": hashlib.sha256((resource_root / "app.asar").read_bytes()).hexdigest(),
        }
    settings_path = product.run_root / "state/electron-user-data/settings.json"
    initial = json.loads(settings_path.read_text())
    check(
        "pre-catalog settings remain readable",
        initial["values"].get("AMADEUS_UI_LOCALE") == "en-US",
    )
    check(
        "startup and installed management leave safety/cache untouched",
        "extensionSecurity" not in initial
        and not (settings_path.parent / "extension-catalog/index.json").exists(),
    )
    code = (ROOT / "electron/tests/fixtures/extensionRelease.cjs").read_text()
    check(
        "isolated shipping-process fixture installed",
        await evaluate_main(
            product,
            f"(async()=>{{{code};return setupReleaseFixture({json.dumps(str(fixture))})}})()",
        ),
    )
    try:
        await button("Plugins")
        theme = "com.example.desktop-theme"
        await install(theme, "0.1.0")
        check("resource installed through shipping UI and IPC", True)
        await configure(True)
        await button("Open appearance settings")
        await page.get_by_role("radio").filter(has_text="Quiet day").click()
        await button("Plugins")
        theme_state = await page.evaluate("window.amadeus.getDesktopTheme()")
        check("theme applied through native frontend owner", bool(theme_state.get("theme")))
        await product.screenshot("release-store-theme")
        result = await page.evaluate(
            "window.amadeus.updateDesktopSettings({values:{AMADEUS_EXTENSION_ARTIFACT_MIRROR:'https://mirror.test/artifacts'}})"
        )
        check("mirror setting accepted", result.get("ok"))
        await evaluate_main(
            product,
            "(()=>{const s=extensionReleaseFixture;s.index={...s.index,sequence:2,entries:s.fixture.index.entries};return true})()",
        )
        await button("Check updates and security status")
        await button("Update plugin…")
        await expect(page.locator(f'[data-extension-id="{theme}"][data-extension-version="0.2.0"]')).to_be_visible(timeout=120000)
        await expect(page.locator(".extension-download-progress")).to_have_count(0, timeout=120000)
        theme_state = await page.evaluate("window.amadeus.getDesktopTheme()")
        check("manual update retains enabled appearance", bool(theme_state.get("theme")))
        requests = await evaluate_main(product, "extensionReleaseFixture.requests")
        check(
            "download uses configured artifact mirror",
            any(url.startswith("/artifacts/") and url.endswith(".whl") for url in requests),
        )
        await evaluate_main(product, "extensionReleaseFixture.offline=true")
        failure = await catalog({"action": "refresh"})
        check(
            "offline refresh retains installed record",
            not failure["ok"] and any(e["id"] == theme for e in failure["catalog"]["installed"]),
        )
        await evaluate_main(product, "extensionReleaseFixture.offline=false")
        await configure(False)
        await page.locator(".extension-details > summary").click()
        await button("Remove")
        await button("Confirm remove")
        await expect(
            page.get_by_text("No community extensions installed.", exact=True)
        ).to_be_visible(timeout=30000)
        fixture_data = json.loads((fixture / "fixture.json").read_text())
        report["fixture_artifacts"] = [
            {key: e[key] for key in ("id", "version", "sha256")}
            for e in fixture_data["index"]["entries"]
        ]
        used = {(theme, "0.1.0"), (theme, "0.2.0")}
        for package_id, kind, name, wheel_name in (
            (
                "com.example.tone-tts",
                "tts",
                "tone",
                "amadeus_example_tone_tts-0.1.0-py3-none-any.whl",
            ),
            (
                "com.example.notes-mcp",
                "mcp",
                "notes",
                "amadeus_example_notes_mcp-0.1.0-py3-none-any.whl",
            ),
        ):
            await install(package_id, "0.1.0")
            await configure(True, "Pi" if kind == "mcp" else None)
            saved = json.loads(settings_path.read_text())["extensions"][package_id]
            if kind == "mcp":
                check(
                    "Notes provider binding saved through shipping UI", saved["providers"] == ["pi"]
                )
            directory = settings_path.parent / "extensions" / saved["installId"]
            record = {
                key: saved[key]
                for key in (
                    "id",
                    "version",
                    "revision",
                    "digest",
                    "manifest",
                    "enabled",
                    "providers",
                )
            }
            record.update(
                package_path=str(directory / "venv" / saved["sitePackages"] / saved["packageRoot"]),
                python_path=str(directory / "venv/Scripts/python.exe"),
                data_path=str(
                    settings_path.parent
                    / "extension-data"
                    / hashlib.sha256(package_id.encode()).hexdigest()
                ),
                config=saved["values"],
                secrets={},
            )
            record_path = product.run_root / (name + "-record.json")
            record_path.write_text(json.dumps(record), encoding="utf-8")
            wheel = fixture_data["wheels"][wheel_name]
            runtime = product.run_root / (name + "-runtime.json")

            def run_contract():
                with (product.run_root / (name + "-contract.log")).open("w") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-I",
                            str(ROOT / "tools/check_extension_contract.py"),
                            "--execute",
                            "--kind",
                            kind,
                            "--wheel",
                            wheel,
                            "--record",
                            str(record_path),
                            "--scenario",
                            str(
                                ROOT / ("examples/extensions/contract-scenarios/" + name + ".json")
                            ),
                            "--output",
                            str(runtime),
                        ],
                        cwd=ROOT,
                        env=product._environment(),
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                    tree = ProcessTree(process)
                    try:
                        return process.wait(timeout=90)
                    finally:
                        tree.terminate()

            check(
                "shipping-installed " + name + " passes native process contract",
                await asyncio.to_thread(run_contract) == 0
                and json.loads(runtime.read_text())["ok"],
            )
            used.add((package_id, "0.1.0"))
            await configure(False)
            await page.locator(".extension-details > summary").click()
            await button("Remove")
            await button("Confirm remove")
        report["used_artifacts"] = [
            entry
            for entry in report["fixture_artifacts"]
            if (entry["id"], entry["version"]) in used
        ]
        await product.screenshot("release-store-process-removed")
        check(
            "native access confirmations exercised",
            await evaluate_main(product, "extensionReleaseFixture.confirmations") >= 5,
        )
        check(
            "legacy settings preserve locale after catalog safety writes",
            json.loads(settings_path.read_text())["values"]["AMADEUS_UI_LOCALE"] == "en-US",
        )
        report["catalog_sequence"] = 2
        return report
    finally:
        (product.run_root / "extension-release-report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
