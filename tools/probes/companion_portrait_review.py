"""Make a self-contained, offline, three-way animation review from experiment assets."""
import argparse
import base64
import json
from pathlib import Path


def data_url(file, mime):
    return f'data:{mime};base64,' + base64.b64encode(file.read_bytes()).decode('ascii')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--compare', nargs=2, default=['matched132','full132'])
    parser.add_argument('--output-name', default='review.html')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    old = json.loads((args.cache / 'manifest.json').read_text(encoding='utf-8'))
    old = {emotion: {mode: [data_url(args.cache / f, 'image/png') for f in frames]
                    for mode, frames in states.items()} for emotion, states in old['emotions'].items()}
    variants = {}
    for name in args.compare:
        manifest = json.loads((args.assets / name / 'manifest.json').read_text(encoding='utf-8'))
        for states in manifest['emotions'].values():
            for spec in states.values():
                spec['url'] = data_url(args.assets / name / spec['url'], 'image/webp')
        variants[name] = manifest
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Companion 原素材补帧对照</title>
<style>__CSS__
html,body{overflow:auto;background:#071b20;height:auto}body{padding:24px;font-size:14px}
h1{font-size:22px;margin:0 0 12px}p{color:#a4c7bf;line-height:1.7}
.controls{display:flex;gap:16px;align-items:center;margin:20px 0}.controls button{width:auto;padding:4px 14px;font-size:14px}
select{color:#dcf4ed;background:#14383c;border:1px solid #397c73;padding:8px}
.cards{display:flex;gap:16px;flex-wrap:wrap}.item{width:470px}.companion-card{height:250px;width:470px}
.item h2{font-size:16px;color:#91dfcc}.note{color:#82aaa0;font-size:12px;min-height:38px}
.portrait canvas{width:100%;height:100%;object-fit:contain}.portrait img{display:none}
#error{color:#ffaaa0;white-space:pre-wrap}button,select{cursor:pointer}
</style><h1>同一构图，三种播放方式</h1>
<p>__DESCRIPTION__<br>此页用于观感比较，页面整体内存不代表单个 Companion。<a style="color:#91dfcc" href="source-variants-v2/review.html">查看原始动画变体</a> · <a style="color:#91dfcc" href="review-edges.html">同源同帧率的边缘对照</a></p>
<div class="controls"><label>表情 <select id="emotion"></select></label><label><input id="speaking" type="checkbox" checked> 说话</label>
<label><input id="staticIdle" type="checkbox"> 候选使用静止待机</label>
<button id="pause">暂停</button><button id="reset">同时重播</button></div><div id="cards" class="cards"></div><p id="error"></p>
<script>__PLAYER__</script><script>
const old=__OLD__, variants=__VARIANTS__;
const names={normal:'普通',sided_thinking:'思考',sided_surprised:'惊讶',happy:'微笑',blush:'害羞',angry:'生气',sad:'难过',disappointed:'失望（旧素材）'};
const selectors=['baseline',...Object.keys(variants)];
const captionMap={baseline:'当前 Companion',matched132:'补帧，保持当前节奏',full132:'完整原动画，约 24 FPS',candidate132:'选源 + 边缘优化（思考 1）',candidate132b:'选源 + 边缘优化（思考 2）'};
const captions=selectors.map(k=>captionMap[k]);
const players=[], canvases=[];let paused=false,oldTimer,oldStart=0,oldElapsed=0,oldFrames=[],ready=false,generation=0;
const loaded=new Map();
for(const [key,label] of Object.entries(names)) emotion.add(new Option(label,key));
for(let i=0;i<3;i++){
 const item=document.createElement('div');item.className='item';
 item.innerHTML=`<h2>${captions[i]}</h2><div class="companion-card"><header><span class="brand">A M A D E U S</span><span class="link-label">PREVIEW</span></header><section class="dialogue"><div class="portrait"><canvas></canvas><div class="scanlines"></div></div><div class="speech"><strong>牧瀬 紅莉栖</strong><p class="caption">先看看这里的线索，我们慢慢来。</p></div></section><footer><span class="signal"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span><span class="status">VOICE</span></footer></div><p class="note"></p>`;
 cards.append(item);const canvas=item.querySelector('canvas');canvases.push(canvas);
 if(i)players.push(new CompanionAtlasPlayer(canvas,variants[selectors[i]],location.href,{softwareCanvas:true}));
}
function oldTick(){
 const elapsed=paused?oldElapsed:performance.now()-oldStart;
 const canvas=canvases[0],ctx=canvas.getContext('2d');ctx.clearRect(0,0,132,132);
 const image=oldFrames[Math.floor(elapsed/170)%oldFrames.length];if(image)ctx.drawImage(image,0,0);
 if(!paused)oldTimer=setTimeout(oldTick,Math.max(1,170-elapsed%170));
}
async function change(){
 const run=++generation;ready=false;clearTimeout(oldTimer);const key=emotion.value,mode=speaking.checked?'speaking':'idle';
 try{
  const frames=await Promise.all(old[key][mode].map(url=>{
   if(!loaded.has(url)){const img=new Image();img.src=url;loaded.set(url,img.decode().then(()=>img));}return loaded.get(url);
  }));
  await Promise.all(players.map(p=>{p.requested=null;return p.select(key,speaking.checked)}));
  if(run!==generation)return;oldFrames=frames;canvases[0].width=canvases[0].height=132;
  oldStart=performance.now();oldElapsed=0;oldTick();
  document.body.classList.toggle('speaking',speaking.checked);
  document.querySelectorAll('.status').forEach(el=>el.textContent=speaking.checked?'VOICE':'STANDBY');
  const notes=document.querySelectorAll('.note');notes[0].textContent=`${oldFrames.length} 帧 · ${oldFrames.length*170} ms / 循环`;
  players.forEach((p,i)=>{p.started=oldStart;p.elapsed=0;p.setPaused(paused);notes[i+1].textContent=`${p.active.sequence.length} 个时间采样 · ${p.active.durationMs} ms / 循环`;});
  ready=true;window.reviewReady=true;
 }catch(e){document.querySelector('#error').textContent=e.stack;window.reviewError=e.stack;}
}
emotion.onchange=change;speaking.onchange=change;reset.onclick=change;
const candidate=variants.candidate132b || variants.candidate132;
const selectedIdle=candidate?.emotions.normal.idle;
staticIdle.disabled=!candidate;
staticIdle.onchange=()=>{candidate.emotions.normal.idle=staticIdle.checked?candidate.emotions.normal.idleStatic:selectedIdle;change();};
pause.onclick=()=>{paused=!paused;if(paused){oldElapsed=performance.now()-oldStart;clearTimeout(oldTimer)}else{oldStart=performance.now()-oldElapsed;oldTick()}
 players.forEach(p=>p.setPaused(paused));pause.textContent=paused?'继续':'暂停';document.querySelectorAll('.signal i').forEach(el=>el.style.animationPlayState=paused?'paused':'running');};
window.reviewSelect=async(key,talk=true)=>{emotion.value=key;speaking.checked=talk;await change();};
window.reviewDraw=(ctx)=>{for(let i=0;i<3;i++){ctx.fillStyle='#0b282e';ctx.fillRect(20+i*200,45,180,180);ctx.drawImage(canvases[i],20+i*200,45,180,180);}};
change();
window.addEventListener('beforeunload',()=>{clearTimeout(oldTimer);players.forEach(p=>p.dispose())});
</script></html>'''
    html = html.replace('__CSS__', (root / 'render/web/companion_panel.css').read_text(encoding='utf-8'))
    if 'candidate132b' in variants:
        description = '左：现有 6 帧。中、右：约 24 FPS，保留各自源素材时长；选择“思考”可对比 3.06 秒的变体 1 和 5.10 秒的变体 2。候选仅稳定透明边缘，保留头部和发丝动作；待机可选静止或 idle_static_saved。'
    elif 'candidate132' in variants:
        description = '左：现有 6 帧。中：旧透明缩放方式。右：修正透明缩放及稳定边缘。说话时中、右使用相同源帧及相同的 24 FPS 时间轴；请注意头发轮廓的明暗闪动。'
    else:
        description = '左：现有 6 帧。中：补齐中间帧，保持 1.02 秒节奏及原有关键帧。右：约 24 FPS，保持源素材完整循环时长。'
    html = html.replace('__DESCRIPTION__', description)
    html = html.replace('__PLAYER__', (root / 'electron/tests/fixtures/companionAtlasPlayer.js').read_text(encoding='utf-8'))
    html = html.replace('__OLD__', json.dumps(old)).replace('__VARIANTS__', json.dumps(variants))
    target = args.assets.parent / args.output_name
    target.write_text(html, encoding='utf-8')
    print(target.resolve())


if __name__ == '__main__':
    main()
