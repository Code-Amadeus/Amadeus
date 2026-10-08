"""Licensed local SDK error/retry, loading race and general art framing checks."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from render.server import AssetServer
from render.visual_profile import VisualProfileStore,draft_profile
from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--core',type=Path,required=True)
    parser.add_argument('--comparison-model',type=Path)
    parser.add_argument('--browser',default='C:/Program Files/Google/Chrome/Application/chrome.exe')
    parser.add_argument('--output',type=Path,default=ROOT/'runtime/live2d-loader')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    output=args.output.resolve();store=VisualProfileStore(output/'profiles.json',project_root=ROOT)
    profile,_=draft_profile(str(args.model));config={'backend':'live2d','selected_profile_id':profile['profile_id'],
        'core_path':str(args.core),'profiles':[profile]};store.save(config)
    server=AssetServer(ROOT,start_port=18660)
    badcore=output/'invalid-core.js';badcore.write_text('window.Live2DCubismCore={};window.PIXI.live2d={};',encoding='utf-8')
    server.mount_files('/probe',{'invalid-core.js':badcore});port=server.start()
    valid=store.runtime_config('render',asset_server=server)
    bad_dir=output/'invalid-model';bad_dir.mkdir(exist_ok=True)
    (bad_dir/'invalid.moc3').write_bytes(b'not-a-cubism-moc')
    (bad_dir/'texture.png').write_bytes(b'not-an-image')
    bad_entry=bad_dir/'invalid.model3.json'
    bad_entry.write_text(json.dumps({'Version':3,'FileReferences':{'Moc':'invalid.moc3','Textures':['texture.png']}}),encoding='utf-8')
    badprofile,_=draft_profile(str(bad_entry))
    store.save({**config,'selected_profile_id':badprofile['profile_id'],'profiles':[profile,badprofile]})
    invalid_model=store.runtime_config('render',asset_server=server)
    report={'checks':{},'limitations':['This qualifies actual SDK/renderer behavior; full Host ownership is qualified by the native driver.']}
    failed=False
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=args.browser,headless=True,timeout=15000,
                args=['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport={'width':360,'height':720});page.set_default_timeout(25000)
            page.add_init_script('window.__DISABLE_RENDERER_WS__=true;')
            requests=[];page.on('request',lambda request:requests.append(request.url))
            page.goto(f'http://127.0.0.1:{port}/render/web/index.html');page.wait_for_function('!!window.renderApp')
            assert page.evaluate('!renderApp._modelAdapter')
            assert not any('pixi-live2d-display' in url or 'live2dcubismcore' in url for url in requests)
            report['checks']['default_sprite_no_sdk_requests']=True
            def configure(value):
                page.evaluate('(config)=>renderApp.configureCharacter(config)',value)
                return page.evaluate('renderApp.getCharacterStatus()')
            first=configure({**valid,'core_url':f'http://127.0.0.1:{port}/probe/invalid-core.js','revision':2})
            assert first['state']=='error' and 'usable Cubism Core' in first.get('error',''),first
            assert page.evaluate('!!PIXI.live2d&&!PIXI.live2d.Live2DModel')
            report['checks']['invalid_core_200_empty_namespace']=first
            ready=configure({**valid,'revision':3});assert ready['state']=='ready',ready
            report['checks']['valid_core_after_invalid']=ready
            bad=configure({**invalid_model,'revision':4});assert bad['state']=='error' and bad.get('error'),bad
            assert page.evaluate('!renderApp._modelAdapter.model&&!renderApp._modelAdapter.texture')
            report['checks']['invalid_model_releases_partial_load']=bad
            ready=configure({**valid,'revision':5});assert ready['state']=='ready',ready
            report['checks']['valid_model_after_invalid']=True
            changed=configure({**valid,'core_url':valid['core_url']+'?different-sdk-source=1','revision':6})
            assert changed['state']=='error' and 'Close and reopen Render or Wallpaper' in changed.get('error',''),changed
            report['checks']['different_loaded_core_requires_reopen']=changed
            ready=configure({**valid,'revision':7});assert ready['state']=='ready'
            page.evaluate("""()=>{const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
             XMLHttpRequest.prototype.open=function(method,url,...rest){this.probeSlow=String(url).includes('.moc3');return open.call(this,method,url,...rest);};
             XMLHttpRequest.prototype.send=function(...args){if(this.probeSlow)setTimeout(()=>send.apply(this,args),600);else send.apply(this,args);};
             window.raceStatuses=[];window.addEventListener('amadeus-character-status',event=>raceStatuses.push(event.detail));}""")
            slow={**valid,'revision':10,'reload_revision':1,'model_url':valid['model_url']+'?reload=slow'}
            latest={**valid,'revision':12,'reload_revision':2,'model_url':valid['model_url']+'?reload=latest'}
            page.evaluate("""([slow,latest])=>{renderApp.configureCharacter(slow);
              setTimeout(()=>renderApp.configureCharacter({...slow,backend:'sprite',revision:11}),30);
              setTimeout(()=>{window.latest=renderApp.configureCharacter(latest);},60);}""",[slow,latest])
            page.wait_for_function("renderApp._characterConfig.revision===12&&renderApp._modelAdapter.state==='ready'&&renderApp._modelAdapter.config.revision===12")
            page.evaluate('window.latest')
            statuses=page.evaluate('raceStatuses');assert not any(item['state']=='ready' and item['revision']==10 for item in statuses),statuses
            assert page.evaluate('renderApp.getTextureStats().residentFrames===0')
            report['checks']['load_sprite_latest_model_race']={'statuses':statuses,'managedTextures':page.evaluate('renderApp.getPixiApp().renderer.texture.managedTextures.length')}
            # Qualify the same framing against a different real exported canvas/art extent.
            if args.comparison_model:
                other,_=draft_profile(str(args.comparison_model));other['layouts']['render']={'scale':.9,'x':0,'y':0}
                store.save({**config,'profiles':[profile,other],'selected_profile_id':other['profile_id']})
                other_config=store.runtime_config('render',asset_server=server)
                other_config['revision']=13
                for width,height in [(360,720),(900,900)]:
                    page.set_viewport_size({'width':width,'height':height})
                    status=configure(other_config);assert status['state']=='ready',status
                    geometry=page.evaluate("""()=>{const a=renderApp._modelAdapter,b=a.artBounds,m=a.model,v=a.viewport;
                      const left=m.x+b.minX*m.scale.x,right=m.x+b.maxX*m.scale.x;
                      const top=m.y+b.minY*m.scale.y,bottom=m.y+b.maxY*m.scale.y;
                      return {left,right,top,bottom,viewport:v,art:b,occupancy:Math.max((right-left)/v.width,(bottom-top)/v.height)};}""")
                    assert abs(geometry['occupancy']-.9)<.000001,geometry
                    assert abs(geometry['left']-(geometry['viewport']['width']-geometry['right']))<.001,geometry
                    page.screenshot(path=str(output/f'comparison-{width}x{height}.png'))
                    report['checks'][f'comparison_framing_{width}x{height}']=geometry
            required={'default_sprite_no_sdk_requests','invalid_core_200_empty_namespace','valid_core_after_invalid',
                'invalid_model_releases_partial_load','valid_model_after_invalid','different_loaded_core_requires_reopen','load_sprite_latest_model_race'}
            if args.comparison_model:required|={'comparison_framing_360x720','comparison_framing_900x900'}
            assert required<=report['checks'].keys()
            browser.close()
    except Exception as error:
        failed=True;report['failure']=type(error).__name__+': '+str(error);print(report['failure'],flush=True)
    finally:
        server.stop();report['success']=not failed
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('SDK lifecycle qualification '+('passed' if not failed else 'failed'),flush=True)
    return 1 if failed else 0

if __name__=='__main__':raise SystemExit(main())
