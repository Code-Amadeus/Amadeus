"""Reviewer-only CSS composition on a known fixture, before the Provider revision.

The temporary browser override is for this isolated design study only. The
Provider receives written decisions and authors the real app CSS itself.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[2]

async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge',headless=True)
        page=await browser.new_page(viewport={'width':1280,'height':900})
        await page.goto('http://127.0.0.1:8767/runtime/auip-style-lab/direct-full-host-f5c97ae295/project/index.html')
        await page.add_style_tag(path=str(Path(__file__).with_name('palette-study.css')))
        await page.screenshot(path=str(ROOT/'runtime/auip-style-lab/palette-study.png'),full_page=True)
        await browser.close()

if __name__=='__main__':
    asyncio.run(main())
