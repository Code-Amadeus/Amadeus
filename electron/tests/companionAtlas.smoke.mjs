// Real production panel + isolated Host, no models/audio. Requires installed Lite media.
import {app,BrowserWindow} from 'electron'
import fs from 'node:fs/promises'
import path from 'node:path'
import assert from 'node:assert/strict'
import {spawn} from 'node:child_process'
import {createInterface} from 'node:readline'
import {fileURLToPath} from 'node:url'
import {CompanionPanel} from '../dist/main/companionPanel.js'

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..')
const output=path.join(root,'output/diagnostics/companion-atlas/production-smoke',new Date().toISOString().replaceAll(':','-'))
await fs.mkdir(output,{recursive:true})
app.setPath('userData',path.join(output,'profile'))
app.on('window-all-closed',()=>{})
let host,bridge,panelHost,panel,slice,seq=0,missing=false
const pending=new Map(),samples=[]
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
const js=code=>panel.webContents.executeJavaScript(code)
async function startHost(){
 host=spawn(path.join(root,'.venv/Scripts/python.exe'),['-u','tools/probes/wallpaper_memory_host.py'],{cwd:root,windowsHide:true,stdio:['pipe','pipe','pipe']})
 host.stderr.on('data',data=>{void fs.appendFile(path.join(output,'host.log'),data)})
 host.stdin.on('error',error=>{void fs.appendFile(path.join(output,'host.log'),String(error))})
 await new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>reject(Error('Host startup timeout')),30000)
  host.once('error',reject)
  createInterface({input:host.stdout}).on('line',line=>{
   let value;try{value=JSON.parse(line)}catch{return}
   if(value.ready){bridge=value;clearTimeout(timer);resolve()}
   else if(pending.has(value.id)){pending.get(value.id)(value.result);pending.delete(value.id)}
  })
 })
}
function rpc(command,params={}){return new Promise((resolve,reject)=>{
 const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(Error('RPC timeout'))},10000)
 pending.set(id,value=>{clearTimeout(timer);resolve(value)})
 host.stdin.write(JSON.stringify({id,command,...params})+'\n')
})}
async function until(code){
 const end=Date.now()+6000
 while(Date.now()<end){if(await js(code))return;await sleep(25)}
 throw Error('Timeout: '+code)
}
async function open(){
 await panelHost.toggle('atlas-smoke')
 panel=BrowserWindow.getAllWindows().find(w=>w.getTitle()==='Amadeus · Companion')
 assert.ok(panel)
 panel.webContents.on('console-message',event=>{void fs.appendFile(path.join(output,'renderer.log'),event.message+'\n')})
 await until(`document.querySelector('#status').textContent==='STANDBY' || document.querySelector('#status').textContent==='VOICE'`)
}
async function sample(stage){
 const processes=app.getAppMetrics().map(p=>({pid:p.pid,type:p.type,memoryKiB:p.memory,cpu:p.cpu}))
 samples.push({stage,processes,rendererPid:panel.webContents.getOSProcessId(),time:Date.now()})
 await fs.writeFile(path.join(output,'samples.json'),JSON.stringify(samples,null,2))
}
async function run(){
 await startHost()
 slice=new BrowserWindow({show:false,webPreferences:{sandbox:true}})
 const options={userDataDir:output,preload:path.join(root,'electron/dist/preload/companion.cjs'),portraitCacheDir:'',bridge:()=>bridge,target:()=>null,slice:()=>slice.webContents}
 panelHost=new CompanionPanel(options)
 slice.webContents.session.webRequest.onBeforeRequest({urls:[`http://127.0.0.1:${bridge.assetPort}/assets/companion/kurisu/manifest.json`]},(_,callback)=>{
  callback(missing?{redirectURL:`http://127.0.0.1:${bridge.assetPort}/missing-companion-pack.json`}:{})
 })
 await open()
 await until(`document.querySelector('#portrait') instanceof HTMLCanvasElement && !document.querySelector('#portrait').hidden`)
 assert.equal(await js('typeof window.PIXI'),'undefined')
 await sleep(2000);await sample('idle')
 await rpc('emotion',{value:'thinking'});await rpc('speaking',{active:true})
 await until(`document.querySelector('#portrait').dataset.emotion==='sided_thinking' && document.body.classList.contains('speaking')`)
 await sleep(1600);await sample('speaking')
 await fs.writeFile(path.join(output,'speaking.png'),(await panel.webContents.capturePage()).toPNG())
 await rpc('speaking',{active:false});const stop=Date.now()
 await sleep(180)
 assert.equal(await js(`document.querySelector('#portrait').dataset.emotion`),'sided_thinking')
 await until(`document.querySelector('#portrait').dataset.emotion==='normal'`)
 const returnMs=Date.now()-stop
 assert.ok(returnMs>=300 && returnMs<1000)
 assert.ok((await js(`document.querySelector('#caption').textContent`)).length>0)
 // A new expression cancels the old expression's delayed return, even before speech.
 await rpc('emotion',{value:'happy'});await rpc('speaking',{active:true});await sleep(80)
 await rpc('speaking',{active:false});await sleep(170)
 await rpc('emotion',{value:'thinking'});await sleep(350)
 assert.equal(await js(`document.querySelector('#portrait').dataset.emotion`),'sided_thinking')
 // A new sentence also cancels the old deadline.
 await rpc('speaking',{active:true});await sleep(80);await rpc('speaking',{active:false});await sleep(170)
 await rpc('speaking',{active:true});await sleep(350)
 assert.equal(await js(`document.querySelector('#portrait').dataset.emotion`),'sided_thinking')
 await rpc('speaking',{active:false});await until(`document.querySelector('#portrait').dataset.emotion==='normal'`)
 await js(`document.querySelector('#motion').click()`)
 await sleep(180)
 const still=await js(`document.querySelector('#portrait').toDataURL()`)
 await sleep(600)
 assert.equal(await js(`document.querySelector('#portrait').toDataURL()`),still)
 await js(`document.querySelector('#motion').click()`)
 panel.hide();await until('document.hidden')
 await sleep(80);const hidden=await js(`document.querySelector('#portrait').toDataURL()`)
 await sleep(500)
 assert.equal(await js(`document.querySelector('#portrait').toDataURL()`),hidden)
 panel.showInactive();await until('!document.hidden');await sleep(400)
 assert.notEqual(await js(`document.querySelector('#portrait').toDataURL()`),hidden)
 const oldPid=panel.webContents.getOSProcessId()
 await panelHost.close();assert.ok(panel.isDestroyed())
 await sleep(300)
 assert.ok(!app.getAppMetrics().some(p=>p.pid===oldPid))
 missing=true;await open()
 assert.equal(await js(`document.querySelector('#fallback').hidden`),false)
 assert.equal(await js(`document.querySelector('#portrait') instanceof HTMLImageElement`),true)
 await panelHost.close()
 // Legacy override is still a real supported caller, using synthetic tiny PNG media.
 const legacy=path.join(output,'legacy');await fs.mkdir(legacy)
 await fs.writeFile(path.join(legacy,'face.png'),Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1cAAAAASUVORK5CYII=','base64'))
 await fs.writeFile(path.join(legacy,'manifest.json'),JSON.stringify({emotions:{normal:{idle:['face.png'],speaking:['face.png']}}}))
 options.portraitCacheDir=legacy;await open()
 await until(`document.querySelector('#portrait').naturalWidth===1`)
 await fs.writeFile(path.join(output,'result.json'),JSON.stringify({ok:true,returnMs,checks:['real Host and production panel','no Pixi','atlas speaking','350ms return','new expression cancels return','new speech cancels return','static idle','hidden pause/resume','renderer exits on close','missing pack fallback','legacy PNG override']}))
}
async function cleanup(){
 await panelHost?.close();for(const w of BrowserWindow.getAllWindows())w.destroy()
 if(host&&host.exitCode===null){host.stdin.end(JSON.stringify({command:'stop'})+'\n');await Promise.race([new Promise(r=>host.once('exit',r)),sleep(5000)]);if(host.exitCode===null)host.kill()}
}
app.whenReady().then(run).then(async()=>{await cleanup();app.exit(0)},async error=>{
 await fs.writeFile(path.join(output,'result.json'),JSON.stringify({ok:false,error:error.stack}));await cleanup();app.exit(1)
})
