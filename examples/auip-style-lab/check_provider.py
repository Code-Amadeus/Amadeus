"""Independent browser evidence against the Provider's actual entry and handlers."""
import argparse
import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

CAPTURE = """(() => {
 let web;
 Object.defineProperty(window, 'AmadeusAUIP', {configurable:true,
 get:()=>web, set:value=>{
   web=value;
   const original=value.createManagedApp;
   value.createManagedApp=function(options){
     window.__authorOptions=options;
     const app=original.call(this,options);
     window.__actualApp=app;
     return app;
   };
 }});
})();"""

def contrast(a, b):
    def luminance(rgb):
        channels=[v/255 for v in rgb]
        linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in channels]
        return sum(v*w for v,w in zip(linear,[.2126,.7152,.0722]))
    high,low=sorted([luminance(a),luminance(b)],reverse=True)
    return (high+.05)/(low+.05)

async def visual_state_matches(page):
    return await page.evaluate("""() => {
      const board=window.__authorOptions.snapshot().board;
      return board.every((tower,i)=> {
        const disks=[...document.querySelectorAll(`#stack-${i} .disk`)]
          .map(e=>({size:Number(e.className.match(/disk-(\\d)/)[1]),y:e.getBoundingClientRect().y}))
          .sort((a,b)=>a.y-b.y).map(e=>e.size);
        return JSON.stringify(disks)===JSON.stringify(tower.disks);
      });
    }""")

async def main(workspace, out):
    out.mkdir(parents=True, exist_ok=True)
    report = {'workspace':str(workspace), 'checks':{}, 'page_errors':[], 'console_warnings':[]}
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='msedge', headless=True)
        context=await browser.new_context(offline=True,viewport={'width':1280,'height':900})
        page=await context.new_page()
        page.on('pageerror',lambda e:report['page_errors'].append(str(e)))
        page.on('console',lambda m:report['console_warnings'].append(m.text) if m.type=='warning' else None)
        await page.add_init_script(CAPTURE)
        await page.goto((workspace/'index.html').as_uri())
        await page.screenshot(path=str(out/'provider-desktop.png'),full_page=True)
        # Compare visual top-to-bottom order to the rules, using actual geometry.
        order=await page.locator('#stack-0 .disk').evaluate_all("els=>els.map(e=>({size:Number(e.className.match(/disk-(\\d)/)[1]),y:e.getBoundingClientRect().y})).sort((a,b)=>a.y-b.y).map(x=>x.size)")
        report['initial_visual_top_to_bottom']=order
        report['checks']['disk_order_matches_rules']=order==[1,2,3]
        report['checks']['real_managed_app_boot']=await page.evaluate('Boolean(window.__actualApp)')
        visual_matches=[await visual_state_matches(page)]
        await page.locator('[data-tower="0"]').click()
        await page.locator('[data-tower="2"]').click()
        report['checks']['local_move']=await page.locator('#move-count').inner_text()=='1'
        visual_matches.append(await visual_state_matches(page))
        before=await page.locator('#tower-board').inner_html()
        await page.keyboard.press('a'); await page.keyboard.press('c')
        report['checks']['illegal_move_no_mutation']=(await page.locator('#move-count').inner_text()=='1' and before==await page.locator('#tower-board').inner_html())
        await page.keyboard.press('r')
        for key in ['a','c','a','b','c','b','a','c','b','a','b','c','a','c']:
            await page.keyboard.press(key)
        report['checks']['keyboard_win_in_seven']=await page.locator('#move-count').inner_text()=='7' and '完成' in await page.locator('#feedback').inner_text()
        visual_matches.append(await visual_state_matches(page))
        report['checks']['visual_matches_initial_move_win']=all(visual_matches)
        await page.locator('#reset-button').click()
        report['checks']['restart_after_win']=await page.locator('#move-count').inner_text()=='0'
        await page.locator('[data-tower="0"]').focus()
        await page.keyboard.press('Tab')
        report['focus']=await page.locator(':focus').evaluate("e=>({outline:getComputedStyle(e).outlineColor,style:getComputedStyle(e).outlineStyle})")
        report['checks']['keyboard_focus_visible']=report['focus']['style']!='none'
        gradient=await page.locator('.playfield').evaluate('e=>getComputedStyle(e).backgroundImage')
        colors=[list(map(int,part.split(',')[:3])) for part in re.findall(r'rgba?\(([^)]+)\)',gradient)]
        outline=list(map(int,re.search(r'rgba?\(([^)]+)\)',report['focus']['outline'])[1].split(',')[:3]))
        report['focus']['contrast_against_board_endpoints']=[round(contrast(outline,c),2) for c in colors]
        report['checks']['focus_contrast']=bool(colors) and all(contrast(outline,c)>=3 for c in colors)
        # This extra core uses the captured real action map and real app state;
        # actual Managed Web construction and top-to-bottom entry already ran.
        await page.reload()
        report['core']=await page.evaluate("""() => {
          const opts=window.__authorOptions;
          const core=AmadeusAUIPManaged.createManagedCore({manifest:opts.manifest,snapshot:opts.snapshot,actions:opts.actions,initialEvents:()=>[]});
          const receipts=[];
          for(const [from,to] of [[0,2],[0,1],[2,1],[0,2],[1,0],[1,2],[0,2]]) {
            const current=core.snapshot();
            receipts.push(core.dispatchAction({type:'hanoi.move',payload:{from,to},expected_revision:core.revision()}));
          }
          const won=core.snapshot();
          const stale=core.dispatchAction({type:'hanoi.reset',payload:{},expected_revision:0});
          const restarted=core.dispatchAction({type:'hanoi.reset',payload:{},expected_revision:core.revision()});
          return {receipts,won,stale,restarted,afterRestart:core.snapshot(),healthy:core.healthy()};
        }""")
        report['checks']['core_primary_loop']=all(r.get('accepted') is True for r in report['core']['receipts'])
        report['checks']['core_stale_rejected']=report['core']['stale'].get('code')=='stale_action_revision'
        report['checks']['core_restart_after_win']=report['core']['restarted'].get('accepted') is True and report['core']['afterRestart']['moves']==0
        report['checks']['no_page_errors']=not report['page_errors']
        await page.set_viewport_size({'width':390,'height':844})
        await page.reload()
        await page.screenshot(path=str(out/'provider-mobile.png'),full_page=True)
        report['checks']['mobile_no_overflow']=await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        await page.emulate_media(reduced_motion='reduce')
        report['checks']['reduced_motion']=await page.locator('#reset-button').evaluate("e=>getComputedStyle(e).transitionDuration==='0s'")
        bare=await context.new_page()
        bare_errors=[]
        bare.on('pageerror',lambda e:bare_errors.append(str(e)))
        await bare.route('**/sdk/**',lambda route:route.abort())
        await bare.goto((workspace/'index.html').as_uri())
        await bare.locator('[data-tower="0"]').click()
        await bare.locator('[data-tower="2"]').click()
        report['checks']['standalone_without_sdk']=await bare.locator('#move-count').inner_text()=='1' and not bare_errors
        report['without_sdk_page_errors']=bare_errors
        report['checks']['no_page_errors']=not report['page_errors']
        await browser.close()
    (out/'checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='core'},ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('workspace',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    asyncio.run(main(args.workspace.resolve(),args.output.resolve()))
