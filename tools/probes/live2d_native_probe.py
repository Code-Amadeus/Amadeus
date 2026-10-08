"""Qualify an isolated actual Host + production Electron renderer instance.

Launch live2d_validation_host.py and the normal compiled Electron entry with
isolated desktop userData/authentication first. This driver uses real RPC and
page subscriptions; only narration/PCM input is deterministic. It never takes
ownership of an existing Lively wallpaper or its recovery journal.
"""
from __future__ import annotations
import argparse
import json
import os
import re
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid
import psutil
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
RPC = """async ([method,params])=>{
 const c=await window.amadeus.getBackendConnection();
 if(!c?.url)throw new Error('Desktop backend connection is unavailable');
 return await new Promise((resolve,reject)=>{
 const id='local-validation-'+Math.random(),w=new WebSocket(c.url,c.protocols);
 const timer=setTimeout(()=>{w.close();reject(new Error('RPC timeout: '+method));},20000);
 w.onopen=()=>w.send(JSON.stringify({type:'req',id,method,params}));
 w.onmessage=event=>{const result=JSON.parse(event.data);if(result.id===id){
 clearTimeout(timer);w.close();if(result.params?.error)reject(new Error(result.params.error));
 else resolve(result.params);}};
 w.onerror=()=>{clearTimeout(timer);reject(new Error('RPC failed: '+method));};});
}"""
READ = """()=>({status:renderApp._modelAdapter?.status(),
 mouth:renderApp._modelAdapter?.smoothedMouth,actual:window.appliedParameters||{},
 sprite:renderApp.getTextureStats(),model:!!renderApp._modelAdapter?.model,
 ticker:renderApp.getPixiApp().ticker.count,
 managedTextures:renderApp.getPixiApp().renderer.texture.managedTextures.length})"""
HOOK = """()=>{const observed=renderApp._modelAdapter.model;window.appliedParameters=null;
 window.capturedModel=observed;observed.internalModel.on('beforeModelUpdate',()=>{
 if(renderApp._modelAdapter.model!==observed)return;
 const core=observed.internalModel.coreModel;
 window.appliedParameters=Object.fromEntries(core._model.parameters.ids.map(id=>[id,core.getParameterValueById(id)]));});}"""
ALPHA = """()=>{
 const app=renderApp.getPixiApp(),texture=renderApp._modelAdapter.texture;
 const pixels=app.renderer.extract.pixels(texture),resolution=texture.baseTexture.resolution;
 const width=Math.round(texture.width*resolution),height=Math.round(texture.height*resolution);
 if(pixels.length!==width*height*4)throw new Error('Unexpected extraction dimensions');
 let left=width,right=-1,top=height,bottom=-1,count=0;
 for(let y=0;y<height;y++)for(let x=0;x<width;x++)if(pixels[(y*width+x)*4+3]>32){
 left=Math.min(left,x);right=Math.max(right,x);top=Math.min(top,y);bottom=Math.max(bottom,y);count++;}
 return {width,height,left,right,top,bottom,opaquePixels:count,
 leftMargin:left,rightMargin:width-1-right,marginDifference:Math.abs(left-(width-1-right))};
}"""


def native_usage(pid):
    parent=psutil.Process(pid)
    processes=[parent,*parent.children(recursive=True)]
    rss=cpu=0
    for process in processes:
        try:
            rss+=process.memory_info().rss
            cpu+=sum(process.cpu_times()[:2])
        except psutil.Error:
            continue
    return {"rss_mb":round(rss/1024**2,2),"cpu_seconds":cpu,"process_count":len(processes)}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--control-directory',type=Path,required=True)
    parser.add_argument('--cdp-url',default='http://127.0.0.1:18555')
    parser.add_argument('--browser',default='C:/Program Files/Google/Chrome/Application/chrome.exe')
    parser.add_argument('--restart-host',action='store_true')
    args=parser.parse_args()
    output=args.control_directory.resolve()
    report={'checks':{},'limitations':[
        'Deterministic existing StreamTagParser/ExpressionController and PCM sink; no paid model or physical audio playback.',
        'Native Electron Render is the actual compiled entry. Windows Electron Slice is the Canvas/composer overlay, not the Live2D scene.',
        'Existing Lively owner/recovery journal was left untouched; native Lively Live2D scene mounting is not qualified.',
        'Short process RSS/frame observations do not prove long-term leak freedom or multi-device performance.',
    ]}
    failed=False
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp(args.cdp_url)
        page=next(p for context in browser.contexts for p in context.pages if 'mainWindow=1' in p.url)
        def rpc(method,params=None):
            return page.evaluate(RPC,[method,params or {}])
        def input_action(action,**params):
            command={'id':str(uuid.uuid4()),'action':action,**params}
            temporary=output/'input.pending.json'
            temporary.write_text(json.dumps(command),encoding='utf-8')
            temporary.replace(output/'input.json')
            deadline=time.monotonic()+6
            while time.monotonic()<deadline:
                page.wait_for_timeout(50)
                try: result=json.loads((output/'input-result.json').read_text(encoding='utf-8'))
                except (FileNotFoundError,json.JSONDecodeError): continue
                if result.get('id')==command['id']:
                    if result.get('ok') is not True: raise RuntimeError(result)
                    return
            raise TimeoutError('Host deterministic input did not complete: '+action)
        def wait_receipts():
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                surfaces=rpc('visual.get')['surfaces']
                if all(surfaces.get(name,{}).get('state')=='ready' for name in ('render','wallpaper')):
                    return surfaces
                page.wait_for_timeout(100)
            raise TimeoutError('Host did not observe both real surface ready receipts')
        def render_frame():
            return next(f for f in page.frames if '/render/web/index.html' in f.url)
        chrome=None
        try:
            frame=render_frame();page.set_default_timeout(30000)
            frame.wait_for_function("renderApp?._modelAdapter?.state==='ready'")
            frame.evaluate(HOOK)
            original=rpc('visual.get')['config']
            assert original['backend']=='live2d'
            live_runtime=frame.evaluate('renderApp._modelAdapter.config.runtime_id')
            wall_start=rpc('wallpaper.start',{'slice_host':'electron'})
            assert wall_start['status'] in {'started','already_running'}
            chrome=pw.chromium.launch(executable_path=args.browser,headless=True,timeout=15000,
                args=['--enable-webgl','--use-angle=d3d11'])
            wall=chrome.new_page(viewport={'width':1600,'height':900});wall.set_default_timeout(30000)
            wall.goto(wall_start['url']);wall.wait_for_function("renderApp?._modelAdapter?.state==='ready'")
            wall.evaluate(HOOK)
            report['checks']['both_ready']=[frame.evaluate(READ),wall.evaluate(READ)]
            wait_receipts()
            input_action('work');input_action('emotion',label='smile')
            for surface in [frame,wall]:
                surface.wait_for_function("window.appliedParameters?.PARAM_EYE_L_SMILE>.9&&renderApp._modelAdapter.expression==='Smile'")
            input_action('mouth')
            for surface in [frame,wall]: surface.wait_for_function('window.appliedParameters?.PARAM_MOUTH_OPEN_Y>.4')
            report['checks']['real_parser_pcm']=[frame.evaluate(READ),wall.evaluate(READ)]
            input_action('end')
            for surface in [frame,wall]: surface.wait_for_function("renderApp._modelAdapter.expression==='Thinking'&&renderApp._modelAdapter.smoothedMouth===0")
            input_action('release-work')
            for surface in [frame,wall]: surface.wait_for_function("renderApp._modelAdapter.expression===''")
            report['checks']['speech_end_restores_source']=True
            for name,surface in [('native_render',frame),('wallpaper_crt',wall)]:
                alpha=surface.evaluate(ALPHA)
                assert alpha['opaquePixels']>1000 and alpha['marginDifference']<=max(4,alpha['width']*.02),alpha
                report['checks'][name+'_alpha_centering']=alpha
            page.screenshot(path=str(output/'native-render-art-bounds.png'))
            wall.screenshot(path=str(output/'real-host-wallpaper-art-bounds.png'))
            root_pid=json.loads((output/'electron-pid.json').read_text())['pid']
            before=native_usage(root_pid)
            frame_timing=frame.evaluate("""async()=>{
             const app=renderApp.getPixiApp(),samples=[];let previous=performance.now();
             return await new Promise(resolve=>{const listener=()=>{const now=performance.now();samples.push(now-previous);previous=now;
             if(samples.length===120){app.ticker.remove(listener);resolve(samples);}};app.ticker.add(listener);
             setTimeout(()=>{app.ticker.remove(listener);resolve(samples);},8000);});}""")
            after=native_usage(root_pid)
            assert len(frame_timing)>=30,'Native ticker did not produce enough visible frames'
            sorted_times=sorted(frame_timing)
            report['native_observations']={'before':before,'after':after,
                'frame_interval_ms':{'samples':len(frame_timing),'median':statistics.median(frame_timing),
                    'p95':sorted_times[int(len(sorted_times)*.95)-1],'max':max(frame_timing)}}
            report['native_observations']['webgl_renderer']=frame.evaluate("""()=>{const gl=renderApp.getPixiApp().renderer.gl,e=gl.getExtension('WEBGL_debug_renderer_info');return e?gl.getParameter(e.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER);}""")
            report['native_observations']['webgl_renderer']=re.sub(r'\s*\(0x[0-9A-Fa-f]+\)', '', report['native_observations']['webgl_renderer'])
            system=browser.new_browser_cdp_session().send('SystemInfo.getInfo')['gpu']
            report['native_observations']['gpu']={'featureStatus':system.get('featureStatus'),
                'devices':[{k:d.get(k) for k in ('vendorString','deviceString','driverVendor','driverVersion')} for d in system.get('devices',[])]}
            sprite={**original,'backend':'sprite'};rpc('visual.save',{'config':sprite})
            frame.wait_for_function("renderApp._mode==='sprite'&&renderApp.getTextureStats().residentFrames>0",timeout=60000)
            report['checks']['real_sprite_loaded']=frame.evaluate(READ)
            frame.locator('body').screenshot(path=str(output/'native-sprite.png'))
            rpc('visual.save',{'config':original});frame.wait_for_function("renderApp._modelAdapter?.state==='ready'&&renderApp._mode==='live2d'")
            frame.wait_for_function('renderApp.getTextureStats().residentFrames===0&&renderApp.getTextureStats().inFlight===0')
            report['checks']['real_sprite_released']=frame.evaluate(READ)
            frame.evaluate(HOOK)
            if args.restart_host:
                input_action('emotion',label='smile');input_action('mouth')
                frame.wait_for_function('renderApp._modelAdapter.smoothedMouth>.4')
                wall.wait_for_function('renderApp._modelAdapter.smoothedMouth>.4')
                old_host=psutil.Process(json.loads((output/'host-pid.json').read_text())['pid'])
                # Kill only the positively identified experimental Host tree.
                command=old_host.cmdline()
                expected_script=(ROOT/'tools/probes/live2d_validation_host.py').resolve()
                if len(command)<2 or Path(command[1]).resolve()!=expected_script:
                    raise RuntimeError('PID does not own this worktree validation Host script')
                if command.count('--control-directory')!=1:
                    raise RuntimeError('Validation Host has no unambiguous control directory')
                control_index=command.index('--control-directory')+1
                if control_index>=len(command) or Path(command[control_index]).resolve()!=output:
                    raise RuntimeError('PID belongs to another validation Host output directory')
                children=old_host.children(recursive=True)
                for process in reversed(children):
                    try: process.terminate()
                    except psutil.NoSuchProcess: pass
                old_host.terminate();psutil.wait_procs([old_host,*children],timeout=5)
                frame.wait_for_function('renderApp._modelAdapter.smoothedMouth===0&&!renderApp._modelAdapter.speaking',timeout=15000)
                wall.wait_for_function('renderApp._modelAdapter.smoothedMouth===0&&!renderApp._modelAdapter.speaking',timeout=15000)
                report['checks']['real_disconnect_closes_mouth']=True
                environment={**os.environ,**json.loads((output/'environment.json').read_text())}
                log=(output/'host-restart-stdio.log').open('w',encoding='utf-8')
                process=subprocess.Popen([sys.executable,str(ROOT/'tools/probes/live2d_validation_host.py'),
                    '--port','17777','--control-directory',str(output)],cwd=ROOT,env=environment,
                    stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
                (output/'host-pid.json').write_text(json.dumps({'pid':process.pid}),encoding='utf-8')
                frame.wait_for_function("renderApp._modelAdapter?.state==='ready'&&renderApp._modelAdapter.config.runtime_id!=="+json.dumps(live_runtime),timeout=45000)
                assert frame.evaluate('renderApp._modelAdapter.smoothedMouth')==0
                frame.evaluate(HOOK)
                frame.wait_for_function("window.capturedModel===renderApp._modelAdapter.model&&window.appliedParameters?.PARAM_MOUTH_OPEN_Y===0&&window.appliedParameters?.PARAM_EYE_L_SMILE<.05")
                report['checks']['same_iframe_host_restart']=frame.evaluate(READ)
                restarted_wall=rpc('wallpaper.start',{'slice_host':'electron'})
                assert restarted_wall['status']=='started'
                wall.wait_for_function("renderApp._modelAdapter?.state==='ready'&&renderApp._modelAdapter.config.runtime_id!=="+json.dumps(live_runtime),timeout=30000)
                assert wall.evaluate('renderApp._modelAdapter.smoothedMouth')==0
                wall.evaluate(HOOK)
                wall.wait_for_function("window.capturedModel===renderApp._modelAdapter.model&&window.appliedParameters?.PARAM_MOUTH_OPEN_Y===0&&window.appliedParameters?.PARAM_EYE_L_SMILE<.05")
                report['checks']['wallpaper_restart_snapshot']=wall.evaluate(READ)
                report['checks']['host_ready_receipts_after_restart']=wait_receipts()
            wallpaper_status=rpc('visual.get')['surfaces']['wallpaper']
            rpc('wallpaper.stop')
            assert rpc('visual.get')['surfaces']['wallpaper']['state']=='unloaded'
            assert rpc('visual.status',wallpaper_status)['accepted'] is False
            report['checks']['wallpaper_stop_late_receipt_rejected']=True
            required={'both_ready','real_parser_pcm','speech_end_restores_source','native_render_alpha_centering',
                'wallpaper_crt_alpha_centering','real_sprite_loaded','real_sprite_released','wallpaper_stop_late_receipt_rejected'}
            if args.restart_host:required|={'real_disconnect_closes_mouth','same_iframe_host_restart','wallpaper_restart_snapshot','host_ready_receipts_after_restart'}
            assert required<=report['checks'].keys(),required-report['checks'].keys()
        except Exception as error:
            failed=True
            report['failure']=type(error).__name__+': '+str(error)
            print(report['failure'],flush=True)
        finally:
            if chrome:chrome.close()
            report['success']=not failed
            (output/'native-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Native/real-Host qualification '+('passed' if not failed else 'failed'),flush=True)
    return 1 if failed else 0

if __name__=='__main__':
    raise SystemExit(main())
