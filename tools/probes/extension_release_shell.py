"""Exercise extension IPC/UI in the shipping shell; synthetic authority and automated dialogs.

Only called by the explicit model-less --extension-fixture smoke. The loopback
inspector belongs to its isolated no-credentials process; production has no test API.
"""
from __future__ import annotations
import asyncio
import hashlib
import http.client
import json
import subprocess
import sys
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
        await socket.send(json.dumps({"id":1,"method":"Runtime.evaluate","params":{
            "expression":guarded,"awaitPromise":True,"returnByValue":True}}))
        while True:
            response = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
            if response.get("id") == 1:
                result = response["result"]
                if result.get("exceptionDetails"):
                    raise RuntimeError(str(result["exceptionDetails"]))
                return result["result"].get("value")


async def exercise(product, fixture: Path):
    from playwright.async_api import expect
    from config.process_tree import ProcessTree
    page = product.page
    report = {"checks":[], "fixture_authority":True,"native_confirmation":"automated",
              "backend":"normal authenticated model-less runtime", "fixture_artifacts":[], "used_artifacts":[]}
    def check(name, value=True):
        if not value:
            raise AssertionError(name)
        report["checks"].append(name)
    async def catalog(request):
        return await page.evaluate("request=>window.amadeus.extensionCatalog(request)",request)
    async def button(name):
        await page.get_by_role("button",name=name,exact=True).click(timeout=30000)
    async def configure(enabled):
        await button("Configure extension")
        await page.locator("#extension-enabled").set_checked(enabled)
        await button("Save extension")
        await expect(page.locator("form.settings-connection-editor")).to_have_count(0,timeout=30000)
    async def install(package_id,version):
        await page.get_by_role("tab",name="Browse",exact=True).click()
        await page.get_by_role("searchbox").fill(package_id)
        await button("View plugin details")
        selection=page.locator(".extension-catalog-detail select")
        if await selection.count():
            await selection.select_option(version)
        await button("Install plugin…")
        await expect(page.locator("#extension-panel-installed")).to_be_visible(timeout=120000)
        await expect(page.get_by_text(package_id+" · "+version,exact=True)).to_be_visible(timeout=120000)
        await expect(page.locator(".extension-download-progress")).to_have_count(0,timeout=120000)
    resource_root = product.packaged_executable.parent / "resources" if product.packaged_executable else ROOT
    checker_root = resource_root / "extension-host" if product.packaged_executable else ROOT
    sdk_directory = resource_root / "extension-sdk" if product.packaged_executable else ROOT / "vendor/extension-sdk"
    report["protocol_pin"] = json.loads((checker_root / "server/extensions/protocol-source.json").read_text())
    report["catalog_pin"] = json.loads((checker_root / "server/extensions/catalog-source.json").read_text())
    if product.packaged_executable:
        report["checker_files_sha256"]={p.relative_to(checker_root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(checker_root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
    report["sdk_sha256"] = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sdk_directory.glob("*.whl")}
    if product.packaged_executable:
        report["build_sha256"] = {"executable":hashlib.sha256(product.packaged_executable.read_bytes()).hexdigest(),
            "app.asar":hashlib.sha256((resource_root / "app.asar").read_bytes()).hexdigest()}
    settings_path=product.run_root/"state/electron-user-data/settings.json"
    initial=json.loads(settings_path.read_text())
    check("pre-catalog settings remain readable", initial["values"].get("AMADEUS_UI_LOCALE")=="en-US")
    check("startup and installed management leave safety/cache untouched", "extensionSecurity" not in initial and not (settings_path.parent/"extension-catalog/index.json").exists())
    code=(ROOT/"electron/tests/fixtures/extensionRelease.cjs").read_text()
    check("isolated shipping-process fixture installed", await evaluate_main(product,f"(async()=>{{{code};return setupReleaseFixture({json.dumps(str(fixture))})}})()"))
    try:
        await button("Settings");await button("General");await button("Manage plugins")
        theme="com.example.desktop-theme"
        await install(theme,"0.1.0")
        check("resource installed through shipping UI and IPC",True)
        await configure(True)
        await button("Open appearance settings")
        await page.get_by_role("radio").filter(has_text="Quiet day").click()
        await button("General");await button("Manage plugins")
        theme_state=await page.evaluate("window.amadeus.getDesktopTheme()")
        check("theme applied through native frontend owner",bool(theme_state.get("theme")))
        await product.screenshot("release-store-theme")
        result=await page.evaluate("window.amadeus.updateDesktopSettings({values:{AMADEUS_EXTENSION_ARTIFACT_MIRROR:'https://mirror.test/artifacts'}})")
        check("mirror setting accepted",result.get("ok"))
        await evaluate_main(product,"(()=>{const s=extensionReleaseFixture;s.index={...s.index,sequence:2,entries:s.fixture.index.entries};return true})()")
        await button("Check updates and security status")
        await button("Update plugin…")
        await expect(page.get_by_text(theme+" · 0.2.0",exact=True)).to_be_visible(timeout=120000)
        await expect(page.locator(".extension-download-progress")).to_have_count(0,timeout=120000)
        theme_state=await page.evaluate("window.amadeus.getDesktopTheme()")
        check("manual update retains enabled appearance",bool(theme_state.get("theme")))
        requests=await evaluate_main(product,"extensionReleaseFixture.requests")
        check("download uses configured artifact mirror",any(url.startswith("/artifacts/") and url.endswith(".whl") for url in requests))
        await evaluate_main(product,"extensionReleaseFixture.offline=true")
        failure=await catalog({"action":"refresh"})
        check("offline refresh retains installed record",not failure["ok"] and any(e["id"]==theme for e in failure["catalog"]["installed"]))
        await evaluate_main(product,"extensionReleaseFixture.offline=false")
        await configure(False)
        await button("Remove");await button("Confirm remove")
        await expect(page.get_by_text("No community extensions installed.",exact=True)).to_be_visible(timeout=30000)
        tone="com.example.tone-tts"
        await install(tone,"0.1.0");await configure(True)
        saved=json.loads(settings_path.read_text())["extensions"][tone]
        directory=settings_path.parent/"extensions"/saved["installId"]
        record={key:saved[key] for key in ("id","version","revision","digest","manifest","enabled","providers")}
        record.update(package_path=str(directory/"venv"/saved["sitePackages"]/saved["packageRoot"]),
            python_path=str(directory/"venv/Scripts/python.exe"),
            data_path=str(settings_path.parent/"extension-data"/hashlib.sha256(tone.encode()).hexdigest()),
            config=saved["values"],secrets={})
        record_path=product.run_root/"tone-record.json";record_path.write_text(json.dumps(record),encoding="utf-8")
        fixture_data=json.loads((fixture/"fixture.json").read_text())
        wheel=next(value for name,value in fixture_data["wheels"].items() if name=="amadeus_example_tone_tts-0.1.0-py3-none-any.whl")
        report["fixture_artifacts"]=[{key:e[key] for key in ("id","version","sha256")} for e in fixture_data["index"]["entries"]]
        report["used_artifacts"]=[entry for entry in report["fixture_artifacts"] if (entry["id"],entry["version"]) in {(theme,"0.1.0"),(theme,"0.2.0"),(tone,"0.1.0")}]
        runtime=product.run_root/"tone-runtime.json"
        def run_contract():
            with (product.run_root/"tone-contract.log").open("w") as log:
                process=subprocess.Popen([sys.executable,"-I",str(ROOT/"tools/check_extension_contract.py"),"--execute","--kind","tts","--wheel",wheel,"--record",str(record_path),"--scenario",str(ROOT/"examples/extensions/contract-scenarios/tone.json"),"--output",str(runtime)],cwd=ROOT,env=product._environment(),stdout=log,stderr=subprocess.STDOUT)
                tree=ProcessTree(process)
                try:
                    return process.wait(timeout=90)
                finally:
                    tree.terminate()
        check("shipping-installed Python package runs through real Runner",await asyncio.to_thread(run_contract)==0 and json.loads(runtime.read_text())["ok"])
        await configure(False);await button("Remove");await button("Confirm remove")
        await product.screenshot("release-store-process-removed")
        check("native access confirmations exercised",await evaluate_main(product,"extensionReleaseFixture.confirmations")>=5)
        check("legacy settings preserve locale after catalog safety writes",json.loads(settings_path.read_text())["values"]["AMADEUS_UI_LOCALE"]=="en-US")
        report["catalog_sequence"]=2
        return report
    finally:
        (product.run_root/"extension-release-report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
