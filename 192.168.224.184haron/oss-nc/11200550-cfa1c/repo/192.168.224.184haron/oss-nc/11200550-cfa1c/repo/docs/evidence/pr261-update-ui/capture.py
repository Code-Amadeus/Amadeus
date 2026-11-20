import json
import sys
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root))
from tools import smoke_electron_model_less as smoke
from tools.probes.extension_release_shell import evaluate_main

exercise = smoke._exercise_renderer
output = root / 'docs/evidence/pr261-update-ui'
output.mkdir(parents=True, exist_ok=True)

async def capture(page, **options):
    checks = await exercise(page, **options)
    product = SimpleNamespace(process=SimpleNamespace(pid=options['process_id']), main_debug_port=options['inspector_port'])
    result = await page.evaluate("window.amadeus.updateDesktopSettings({values:{AMADEUS_UI_LOCALE:'zh-CN'}})")
    assert result['ok'], result
    await page.reload()
    await page.wait_for_function("document.documentElement.lang === 'zh-CN'")
    await page.get_by_role('button', name='插件', exact=True).click()
    await page.get_by_role('heading', name='插件更新', exact=True).wait_for(state='visible')
    await page.evaluate('document.fonts.ready')
    await page.screenshot(path=str(output / 'live-default-zh.png'), full_page=False, scale='css')
    snapshot = await page.evaluate('window.amadeus.getDesktopSettings()')
    window_url = page.url
    guard = "const electron=process.getBuiltinModule('module').createRequire(process.execPath)('electron');"
    await evaluate_main(product, "(()=>{" + guard + "globalThis.__pr261Window=electron.BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===" + json.dumps(window_url) + ");if(!globalThis.__pr261Window)throw Error('Owned window missing');electron.ipcMain.removeHandler('extensions.catalog');electron.ipcMain.handle('extensions.catalog',()=>globalThis.__pr261Catalog);return true})()")
    package = 'org.example.voice'
    ready = dict(id=package, version='1.1.0', previousVersion='1.0.0', canConfirm=True, recoveryRequired=False)
    records = []

    async def show(name, extra, heading, *, narrow=False):
        # Only catalog data is synthetic. The app, renderer, theme provider,
        # window and backend are the normal isolated smoke application.
        state = dict(entries=[], cached=False, autoCheck=True, **extra)
        settings = dict(snapshot, extensionSecurity=dict(status='ready', checkedAt='2026-10-25T04:00:00Z'))
        reply = dict(ok=True, catalog=state, settings=settings)
        await evaluate_main(product, "(()=>{globalThis.__pr261Catalog=" + json.dumps(reply) + ";globalThis.__pr261Window.setSize(" + ('600,700' if narrow else '1100,800') + ");globalThis.__pr261Window.webContents.send('desktop-theme.changed');return true})()")
        await page.wait_for_function('innerWidth < 650' if narrow else 'innerWidth > 1000')
        await page.get_by_role('heading', name=heading, exact=True).wait_for(state='visible')
        await page.evaluate('document.fonts.ready')
        if narrow:
            await page.locator('.extension-update-status').last.scroll_into_view_if_needed()
        dimensions = await page.evaluate("({width:innerWidth,height:innerHeight,overflow:[...document.querySelectorAll('.settings-main,.settings-scroll-area,.extension-update-status')].filter(e=>e.scrollWidth>e.clientWidth+1).map(e=>e.className)})")
        assert not dimensions['overflow'], dimensions
        assert await page.locator('[role=alert]').count() == 0, await page.locator('[role=alert]').all_text_contents()
        await page.screenshot(path=str(output / f'{name}.png'), full_page=False, scale='css')
        records.append(dict(name=name, **dimensions))

    await show('live-scheduled-zh', dict(scheduledInstall=dict(id=package, version='1.1.0', digest='b'*64)), '已预约安装')
    await show('live-ready-zh', dict(pendingUpdate=ready), '更新待确认')
    result = await page.evaluate("window.amadeus.updateDesktopSettings({values:{AMADEUS_UI_THEME:'wallpaper-slice'}})")
    assert result['ok'], result
    await page.wait_for_function("document.documentElement.dataset.theme === 'wallpaper-slice'")
    await show('live-ready-dark-zh', dict(pendingUpdate=ready), '更新待确认')
    result = await page.evaluate("window.amadeus.updateDesktopSettings({values:{AMADEUS_UI_THEME:'classic'}})")
    assert result['ok'], result
    await page.wait_for_function("document.documentElement.dataset.theme === 'classic'")
    recovery = dict(ready, id='org.example.long-package-identity-for-layout-checks', canConfirm=False, recoveryRequired=True)
    await show('live-recovery-narrow-zh', dict(pendingUpdate=recovery, rollbackScheduled=True), '需要恢复', narrow=True)
    (root / 'build/pr261-update-ui-viewports.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
    checks['update_ui_live_captures'] = True
    return checks

smoke._exercise_renderer = capture
sys.argv = ['smoke_electron_model_less.py', '--report-dir', str(root / 'build/pr261-update-ui-smoke')]
raise SystemExit(smoke.main())
