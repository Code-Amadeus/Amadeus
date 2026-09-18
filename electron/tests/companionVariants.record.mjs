import {app,BrowserWindow} from 'electron'
import fs from 'node:fs/promises'
import path from 'node:path'
const file=path.resolve(process.argv[2]),output=path.dirname(file)
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
app.setPath('userData',path.join(output,'record-profile'))
let window
app.whenReady().then(async()=>{
  window=new BrowserWindow({width:1460,height:1040,show:false,webPreferences:{offscreen:true,backgroundThrottling:false,sandbox:true,contextIsolation:true}})
  window.webContents.setFrameRate(30);window.webContents.on('paint',()=>{})
  await window.loadFile(file)
  const deadline=Date.now()+30000
  while(!await window.webContents.executeJavaScript('Boolean(window.reviewReady)')){
    if(Date.now()>deadline)throw Error(await window.webContents.executeJavaScript('document.querySelector("#error").textContent || "timeout"'))
    await sleep(100)
  }
  for(const [group,name] of [['正面待机','idle'],['普通讲话','speaking'],['思考讲话','thinking'],['侧面待机','side']]){
    await window.webContents.executeJavaScript(`reviewGroup(${JSON.stringify(group)})`)
    await sleep(500)
    await fs.writeFile(path.join(output,`${name}.png`),(await window.webContents.capturePage()).toPNG())
  }
  // A compact motion study: two fixed-head idle choices + two thinking variants.
  await window.webContents.executeJavaScript(`(async()=>{
    players.forEach(p=>p.dispose());players=[];grid.replaceChildren();
    const choices=['kurisu_idle_static_defualt','kurisu_idle_static_saved','kurisu_thinking_speaking_loop','kurisu_thinking_speaking_loop2'];
    const canvases=[];
    for(const name of choices){const item=items.find(i=>i.project===name),canvas=document.createElement('canvas');grid.append(canvas);canvases.push(canvas);
      const p=new CompanionAtlasPlayer(canvas,{emotions:{normal:{idle:item.spec,speaking:item.spec}}},location.href,{softwareCanvas:true});players.push(p);await p.select('normal',false);}
    const view=document.createElement('canvas');view.width=800;view.height=260;document.body.append(view);const c=view.getContext('2d');
    const labels=['idle default','idle static saved','thinking 1','thinking 2'];
    const draw=()=>{c.fillStyle='#0b2228';c.fillRect(0,0,800,260);c.fillStyle='#d6f4e9';c.font='16px Arial';canvases.forEach((source,i)=>{c.fillText(labels[i],i*200+12,24);c.drawImage(source,i*200+12,45,176,176)});};draw();
    const timer=setInterval(draw,1000/24),stream=view.captureStream(24),chunks=[];
    const recorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp9',videoBitsPerSecond:2400000});
    window.movie={recorder,timer,stream,done:new Promise(resolve=>{recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};recorder.onstop=()=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.readAsDataURL(new Blob(chunks,{type:'video/webm'}));};})};recorder.start(1000);
  })()`)
  await sleep(11000)
  const data=await window.webContents.executeJavaScript('(movie.recorder.stop(),movie.done)')
  await fs.writeFile(path.join(output,'variants.webm'),Buffer.from(data,'base64'))
  await window.webContents.executeJavaScript('clearInterval(movie.timer);movie.stream.getTracks().forEach(t=>t.stop())')
  await fs.writeFile(path.join(output,'record-result.json'),JSON.stringify({ok:true}))
  window.destroy();app.exit(0)
}).catch(async e=>{await fs.writeFile(path.join(output,'record-result.json'),JSON.stringify({ok:false,error:e.stack}));window?.destroy();app.exit(1)})
