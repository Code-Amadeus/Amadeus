"""Inspect authoring variants, including ones absent from the installed runtime graph."""
import argparse
import base64
import json
import math
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageChops, ImageDraw

from build_companion_portraits import atlas, timing_indices
from refine_companion_edges import crop_alpha_correct, stabilize_edges


GROUPS = {
    '正面待机': [
        'kurisu_idle_static_defualt', 'kurisu_idle_static_saved', 'kurisu_idle_static2_saved',
        'kurisu_idle_frozen_portrait_4s_1777637268329', 'kurisu_idle_no_nod_4s_1777637049214',
        'kurisu_idle_breath_blink_saved', 'kurisu_idle_micro_wind_saved',
        'kurisu_idle_viewer_right_soft_saved', 'kurisu_idle_character_right_soft_saved'],
    '侧面待机': ['kurisu_side_idle_breath','kurisu_side_idle_soft_wind','kurisu_side_idle_wind'],
    '普通讲话': ['kurisu_idle_speaking_short','kuisu_idle_speaking_medium','kurisu_idle_speaking_long'],
    '思考讲话': ['kurisu_thinking_speaking_loop','kurisu_thinking_speaking_loop2'],
    '害羞讲话': ['kurisu_shy_speaking_loop','kurisu_shy_speaking_loop2'],
    '微笑待机': ['kurisu_smile_loop','kurisu_smile_idle_4s_1777871588446'],
}


def motion_estimate(frames):
    # Translation of a fixed mid-face template; not a landmark/head-pose detector.
    # Keep mouth outside the ROI. Lighting, blinks and artwork deformation can bias it.
    samples=[np.asarray(frames[i].convert('L')).astype(float) for i in np.linspace(0,len(frames)-1,min(12,len(frames))).astype(int)]
    reference=samples[0][47:87,40:95]
    reference-=reference.mean()
    shifts=[]
    for frame in samples:
        scores=[]
        for dy in range(-5,6):
            for dx in range(-5,6):
                roi=frame[47+dy:87+dy,40+dx:95+dx]
                diff=(roi-roi.mean())-reference
                scores.append((float(np.mean(diff*diff)),dx,dy))
        _,x,y=min(scores)
        shifts.append([x,y])
    return {'translationPixels':shifts,'maxTranslationPx':max(math.hypot(x,y) for x,y in shifts),
            'caveat':'Integer mid-face template translation; blinks/lighting/deformation can bias it. Review animation.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--ffprobe',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    graph=json.loads((args.workspace/'graph_config.json').read_text())
    runtime=json.loads(args.runtime.read_text())
    items=[]
    for group,projects in GROUPS.items():
        for project in projects:
            project_root=args.workspace/'projects'/project
            candidates=[]
            for frame_dir in ['frames_alpha_2x_gmfss','frames_alpha']:
                for state in ['idle','speaking']:
                    directory=project_root/frame_dir/state/'loop'
                    files=sorted(directory.glob('*.png'))
                    if files:candidates.append((directory,files))
            if not candidates:
                items.append({'group':group,'project':project,'error':'no PNG loop'});continue
            directory,files=candidates[0]
            label=next((n['label'] for n in graph['nodes'] if (args.workspace/n['root']).resolve()==directory.parent.resolve()),None)
            clip=runtime['clips'].get(label)
            if clip and [f.stem for f in files]==[Path(f).stem for f in clip['frames']]:
                duration=len(files)*clip['frameIntervalMs'];timing='runtime_manifest'
            else:
                video=next((project_root/'downloads').glob('*.mp4'),None)
                if video is None:
                    items.append({'group':group,'project':project,'error':'no timing metadata'});continue
                info=json.loads(subprocess.check_output([str(args.ffprobe),'-v','quiet','-select_streams','v:0',
                    '-show_entries','stream=avg_frame_rate,duration,nb_frames','-of','json',str(video)]))['streams'][0]
                numerator,denominator=map(int,info['avg_frame_rate'].split('/'))
                originals=list((project_root/'frames_alpha'/directory.parent.name/'loop').glob('*.png'))
                duration=len(originals)*1000*denominator/numerator;timing='original PNG count / video FPS'
            indices,_=timing_indices(len(files),duration/len(files),24)
            try:
                frames=[crop_alpha_correct(Image.open(files[i]).convert('RGBA'),132,.74,.035) for i in indices]
            except (OSError, ValueError) as error:
                items.append({'group':group,'project':project,'directory':str(directory),'error':str(error)})
                print(json.dumps({'project':project,'skipped':str(error)}),flush=True)
                continue
            for frame in frames:
                mask=Image.new('L',(132,132));ImageDraw.Draw(mask).rounded_rectangle((0,0,131,131),18,fill=255)
                frame.putalpha(ImageChops.multiply(frame.getchannel('A'),mask))
            # Use the same colour-stability strength as the accepted portrait cache.
            arrays=np.stack([np.asarray(f).astype(np.float32) for f in frames])
            means=np.stack([a[:,:,:3][a[:,:,3]>220].mean(axis=0) for a in arrays])
            target=np.median(means,axis=0)
            for i,a in enumerate(arrays):
                a[:,:,:3]+=(target-means[i])*.92*a[:,:,3:4]/255
            frames=[Image.fromarray(np.clip(a,0,255).astype(np.uint8)) for a in arrays]
            frames,_=stabilize_edges(frames)
            spec=atlas(args.output,project,frames,duration)
            item={'group':group,'project':project,'label':label,'directory':str(directory),
                  'sourceFrames':len(files),'timing':timing,'spec':spec,**motion_estimate(frames)}
            items.append(item)
            print(json.dumps({'project':project,'maxFaceShiftPx':item['maxTranslationPx'],'frames':len(frames)}),flush=True)
    (args.output/'inventory.json').write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding='utf-8')
    root=Path(__file__).resolve().parents[2]
    player=(root/'electron/tests/fixtures/companionAtlasPlayer.js').read_text()
    payload=[]
    for item in items:
        if 'error' in item:continue
        entry=json.loads(json.dumps(item));entry['spec']['url']='data:image/webp;base64,'+base64.b64encode((args.output/item['spec']['url']).read_bytes()).decode()
        payload.append(entry)
    html='''<!doctype html><meta charset="utf-8"><title>Companion 动画源变体</title>
<style>body{background:#0b2228;color:#d6f4e9;font:15px system-ui;margin:24px}h1{font-size:22px}p{max-width:1050px;line-height:1.7;color:#9fc9bd}select,button{background:#153c40;color:#d6f4e9;padding:8px;border:1px solid #438477;cursor:pointer}#grid{display:flex;flex-wrap:wrap;gap:18px;margin-top:22px}.card{background:#102e34;padding:14px;border:1px solid #315b56;border-radius:10px;width:300px}canvas{width:198px;height:198px;display:block;margin:14px auto}small{word-break:break-all;color:#9fc9bd}.card strong{display:block;font-size:13px;word-break:break-all}label{margin-left:15px}</style>
<h1>先选动画源，再决定 Companion 使用哪一版</h1><p>全部取自本地原始 PNG，保持原始循环时长。可比较同表情不同变体；名称中的 static / frozen 仅是素材名称。位移是鼻眼区域模板匹配的粗略估计，不等于头部追踪。头发、眨眼和嘴型以播放观感为准。</p>
<select id="group"></select><button id="pause">暂停 / 继续</button><label><input type="checkbox" id="large" checked> 放大查看边缘</label><div id="grid"></div><p id="error"></p>
<script>__PLAYER__</script><script>const items=__ITEMS__;let players=[],paused=false;
for(const name of [...new Set(items.map(i=>i.group))])group.add(new Option(name,name));
async function show(){players.forEach(p=>p.dispose());players=[];grid.replaceChildren();for(const item of items.filter(i=>i.group===group.value)){
 const card=document.createElement('div');card.className='card';const heading=document.createElement('strong');heading.textContent=item.project;card.append(heading);
 const canvas=document.createElement('canvas');card.append(canvas);const small=document.createElement('small');small.textContent=`${item.spec.sequence.length} 采样 · ${(item.spec.durationMs/1000).toFixed(2)}秒 · 模板位移≤${item.maxTranslationPx.toFixed(1)}px`;card.append(small);grid.append(card);
 const p=new CompanionAtlasPlayer(canvas,{emotions:{normal:{idle:item.spec,speaking:item.spec}}},location.href,{softwareCanvas:true});players.push(p);await p.select('normal',false);p.setPaused(paused);
}large.onchange();window.reviewReady=true;}
group.onchange=()=>show().catch(e=>error.textContent=e.stack);pause.onclick=()=>{paused=!paused;players.forEach(p=>p.setPaused(paused))};large.onchange=()=>{document.querySelectorAll('canvas').forEach(c=>c.style.width=c.style.height=large.checked?'198px':'132px')};
window.reviewGroup=async(value)=>{group.value=value;await show();};show().catch(e=>error.textContent=e.stack);</script>'''
    (args.output/'review.html').write_text(html.replace('__PLAYER__',player).replace('__ITEMS__',json.dumps(payload)),encoding='utf-8')


if __name__=='__main__':main()
