"""Render and exercise the local visual reference; output is experiment evidence."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parents[1] / "runtime" / "auip-style-lab"

async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto((ROOT / "reference.html").as_uri())
        await page.screenshot(path=str(OUT / "reference-desktop.png"), full_page=True)
        await page.locator('#kelvin').fill('8000')
        await page.locator('#gain').fill('50')
        await page.locator('#source').select_option('band')
        await page.get_by_role('button', name='记录观察').click()
        assert await page.locator('#temperature').inner_text() == '8000'
        assert '8000 K / 50%' in await page.locator('#saved').inner_text()
        await page.get_by_role('button', name='重置').click()
        assert await page.locator('#kelvin').input_value() == '4200'
        await page.locator('#kelvin').focus()
        await page.keyboard.press('ArrowRight')
        assert await page.locator('#temperature').inner_text() == '4300'
        focus = await page.locator('#kelvin').evaluate('(e) => getComputedStyle(e).outlineStyle')
        assert focus != 'none'
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.get_by_role('button', name='重置').click()
        await page.screenshot(path=str(OUT / "reference-mobile.png"), full_page=True)
        overflow = await page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not overflow
        await page.emulate_media(reduced_motion='reduce')
        transition = await page.locator('#save').evaluate('(e) => getComputedStyle(e).transitionDuration')
        assert transition == '0s'
        assert not errors, errors
        report = {"primary_interactions": "passed", "keyboard_range": "passed", "focus_outline": focus,
                  "mobile_overflow": overflow, "reduced_motion_transition": transition, "page_errors": errors}
        (OUT / 'reference-checks.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
