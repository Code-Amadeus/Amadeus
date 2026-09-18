// Capture review artifacts outside all performance measurement phases.
import {app,BrowserWindow} from 'electron'
import fs from 'node:fs/promises'
import path from 'node:path'
const file=path.resolve(process.argv[2])
const output=path.dirname(file)
const finalReview=path.basename(file)==='review-final.html'
const prefix=finalReview?'final-':' '
app.setPath('userData',path.join(output,'record-profile'))
let window
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
app.whenReady().then(async()=>{
  window=new BrowserWindow({width:1510,height:520,show:false,webPreferences:{offscreen:true,backgroundThrottling:false,sandbox:true,contextIsolation:true}})
  window.webContents.setFrameRate(60)
  window.webContents.on('paint',()=>{})
  window.webContents.on('console-message',e=>{void fs.appendFile(path.join(output,'record.log'),e.message+'\n')})
  await window.loadFile(file)
  const deadline=Date.now()+30000
  while(!await window.webContents.executeJavaScript('Boolean(window.reviewReady)')){
    const error=await window.webContents.executeJavaScript('window.reviewError || null')
    if(error)throw Error(error)
    if(Date.now()>deadline)throw Error('review startup timeout')
    await sleep(100)
  }
  await fs.writeFile(path.join(output,`${prefix.trim()}review.png`),(await window.webContents.capturePage()).toPNG())
  await window.webContents.executeJavaScript(`window.recordLabels=${JSON.stringify(finalReview?['原版 6 帧','候选·思考 1','候选·思考 2']:['当前 6 帧','补帧·相同节奏','完整循环·24 FPS'])}`)
  await window.webContents.executeJavaScript(`(() => {
    const canvas=document.createElement('canvas');canvas.width=620;canvas.height=280;
    document.body.append(canvas);const ctx=canvas.getContext('2d');
    window.movieLabel='普通 / normal';const labels=window.recordLabels;
    const draw=()=>{ctx.fillStyle='#071b20';ctx.fillRect(0,0,620,280);ctx.fillStyle='#91dfcc';ctx.font='16px "Microsoft YaHei"';
      labels.forEach((label,i)=>ctx.fillText(label,20+i*200,26));reviewDraw(ctx);ctx.fillStyle='#d6f4e9';ctx.fillText(movieLabel,20,256);};
    draw();const timer=setInterval(draw,1000/24);const stream=canvas.captureStream(24),chunks=[];
    const recorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp9',videoBitsPerSecond:2200000});
    window.movie={recorder,timer,stream,done:new Promise(resolve=>{recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
      recorder.onstop=()=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.readAsDataURL(new Blob(chunks,{type:'video/webm'}))};})};
    recorder.start(1000);
  })()`)
  for(const [emotion,label,ms,speaking] of [['normal','待机 / idle',5200,false],['normal','普通 / normal',4200,true],['sided_thinking','思考变体 / thinking variants',11000,true],['happy','微笑 / happy',4000,true],['blush','害羞 / shy',5500,true]]){
    await window.webContents.executeJavaScript(`(movieLabel=${JSON.stringify(label)},reviewSelect(${JSON.stringify(emotion)},${speaking}))`)
    await sleep(ms)
  }
  const data=await window.webContents.executeJavaScript('(movie.recorder.stop(),movie.done)')
  await fs.writeFile(path.join(output,`${prefix.trim()}comparison.webm`),Buffer.from(data,'base64'))
  await window.webContents.executeJavaScript('clearInterval(movie.timer);movie.stream.getTracks().forEach(t=>t.stop())')
  await fs.writeFile(path.join(output,'record-result.json'),JSON.stringify({ok:true}))
  window.destroy();app.exit(0)
}).catch(async e=>{await fs.writeFile(path.join(output,'record-result.json'),JSON.stringify({ok:false,error:e.stack}));window?.destroy();app.exit(1)})
