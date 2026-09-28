"""Check the review surface and capture the existing Preview frame with both apps."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

OUT=Path(__file__).resolve().parents[2]/'runtime'/'auip-style-lab'

async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1120})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        response=await page.goto('http://127.0.0.1:8767/examples/auip-style-lab/')
        assert "connect-src 'none'" in response.headers['content-security-policy']
        await page.frame_locator('#content').locator('#temperature').wait_for()
        await page.screenshot(path=str(OUT/'lab-reference.png'),full_page=True)
        await page.locator('#provider').click()
        frame=page.frame_locator('#content')
        await frame.locator('#move-count').wait_for()
        await page.screenshot(path=str(OUT/'lab-provider.png'),full_page=True)
        await frame.locator('[data-tower="0"]').click()
        await frame.locator('[data-tower="2"]').click()
        assert await frame.locator('#move-count').inner_text()=='1'
        await page.locator('#frame').uncheck()
        assert await page.locator('#shell').evaluate("e=>e.classList.contains('unframed')")
        assert await frame.locator('#move-count').inner_text()=='1'
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        await page.screenshot(path=str(OUT/'lab-mobile.png'),full_page=True)
        assert not errors,errors
        (OUT/'lab-checks.json').write_text(json.dumps({'frame_toggle_preserves_app_state':True,'revised_primary_interaction':True,'mobile_no_overflow':True,'csp_blocks_connections':True,'page_errors':errors},indent=2),encoding='utf-8')
        print('Review page passed: switching, frame toggle without reset, primary interaction, 390px, CSP, no page errors.')
        await browser.close()

if __name__=='__main__':
    asyncio.run(main())
