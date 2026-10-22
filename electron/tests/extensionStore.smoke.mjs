/** Production store React + IPC dispatcher + Electron net + real wheel transactions. */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import https from 'node:https'
import { createHash, X509Certificate } from 'node:crypto'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { app, BrowserWindow, ipcMain, session } from 'electron'
import { DesktopSettingsStore } from '../dist/main/desktopSettings.js'
import { ExtensionInstaller, ExtensionPackageHelper } from '../dist/main/extensions.js'
import { ExtensionCatalog, OFFICIAL_EXTENSION_INDEX } from '../dist/main/extensionCatalog.js'
import { downloadExtension } from '../dist/main/extensionDownloads.js'
const root = fileURLToPath(new URL('../../', import.meta.url))
const base = path.resolve(process.env.AMADEUS_CATALOG_JOURNEY_ROOT)
assert.ok(path.relative(path.join(root,'build/extension-catalog-journey'),base) && !path.relative(path.join(root,'build/extension-catalog-journey'),base).startsWith('..'))
const fixture = JSON.parse(fs.readFileSync(path.join(base,'fixture.json')))
const python = process.env.AMADEUS_TEST_PYTHON
const environment = {...process.env, PIP_CONFIG_FILE:process.platform==='win32'?'NUL':'/dev/null'}
const helper = new ExtensionPackageHelper(root,()=>python,environment)
const store = new DesktopSettingsStore(path.join(base,'settings.json'),path.join(base,'.env'),process.platform,helper.validateRecord)
let window, vite, origin, offline=false, badWheel=false, slow=true, confirms=0
let index = {...fixture.index, entries:fixture.index.entries.filter(e=>e.version==='0.1.0')}
const themeId='com.example.desktop-theme', toneId='com.example.tone-tts'
const malicious='<img src="https://untrusted.invalid/pixel.png" onerror="globalThis.catalogInjected=true"> **plain**'
index.entries.find(e=>e.id===themeId).display.description=malicious
index.entries.find(e=>e.id===themeId).display.homepage='javascript:alert(1)'
index.entries.find(e=>e.id===themeId).display.repository='https://example.test/source'
const report={ok:false, checks:[], artifacts:fixture.index.entries.map(e=>({id:e.id,version:e.version,sha256:e.sha256})), nativeDialogs:false, fixtureAuthority:true, fullBackend:false, screenshots:[]}
const check = (name, value=true)=>{assert.ok(value,name);report.checks.push(name);console.log('store-check:',name)}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms))
const notify=()=>{if(window&&!window.isDestroyed())window.webContents.send('store.theme.changed')}
const installer = new ExtensionInstaller(path.join(base,'extensions'),root,store,helper,()=>python,environment,undefined,async()=>{confirms++;await pause(450);return true},undefined,undefined,notify)
const network=[];const opened=[]
const server=https.createServer({key:fs.readFileSync(path.join(base,'key.pem')),cert:fs.readFileSync(path.join(base,'cert.pem'))},(request,response)=>{
 network.push(request.url)
 if(offline){response.writeHead(503);response.end();return}
 if(request.url==='/index.json'){response.end(JSON.stringify(index));return}
 const wheel=fixture.wheels[decodeURIComponent(request.url.split('/').at(-1))]
 if(!wheel){response.writeHead(404);response.end();return}
 let bytes=fs.readFileSync(wheel)
 if(badWheel&&wheel.includes('theme-0.2.0'))bytes=Buffer.alloc(bytes.length,1)
 response.writeHead(200,{'Content-Length':bytes.length})
 if(slow){const half=Math.floor(bytes.length/2);response.write(bytes.subarray(0,half));const timer=setTimeout(()=>response.end(bytes.subarray(half)),1500);response.on('close',()=>clearTimeout(timer))}
 else response.end(bytes)
})
app.setPath('userData',path.join(base,'ui-profile'))
app.on('window-all-closed',()=>{})
process.on('uncaughtException',e=>{console.error(e);app.exit(1)})
app.whenReady().then(async()=> {
const bounded=(promise, label)=>Promise.race([promise,new Promise((_,reject)=>{const timer=setTimeout(()=>reject(new Error(label)),15000);timer.unref()})])
const ui=source=>bounded(window.webContents.executeJavaScript(source),'Renderer query timed out: '+source)
async function until(source,message){const deadline=Date.now()+90000;while(Date.now()<deadline){if(await ui(source))return;const errors=await ui('window.extensionFixture?.errors || []');if(errors.length)throw new Error('Renderer error: '+errors.join('; '));await pause(80)}throw new Error(message)}
async function click(label){await until(`(()=>{const n=Array.from(document.querySelectorAll('button')).find(n=>n.getClientRects().length&&!n.disabled&&n.textContent.trim()===${JSON.stringify(label)});if(!n)return false;n.click();return true})()`,'Button unavailable: '+label);await pause(70)}
async function input(selector,value){await ui(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(n,${JSON.stringify(value)});n.dispatchEvent(new Event('input',{bubbles:true}))})()`);await pause(70)}
async function capture(name){const image=await window.webContents.capturePage(undefined,{stayHidden:true,stayAwake:true});fs.writeFileSync(path.join(base,name+'.png'),image.toPNG());report.screenshots.push(name+'.png')}
async function installed(id,version){await until(`document.querySelector('#extension-panel-installed:not([hidden])')&&Array.from(document.querySelectorAll('[data-extension-id]')).some(n=>n.dataset.extensionId===${JSON.stringify(id)}&&n.dataset.extensionVersion===${JSON.stringify(version)})&&!document.querySelector('.extension-download-progress')`,'Install did not finish: '+id)}
async function configure(enabled){await until(`(()=>{const n=document.querySelector('#extension-panel-installed [role="switch"][id^="extension-enabled-"]');if(!n||n.disabled)return false;if((n.getAttribute('aria-checked')==='true')!==${enabled})n.click();return true})()`,'Card switch unavailable');await until(`document.querySelector('#extension-panel-installed [role="switch"][id^="extension-enabled-"]').getAttribute('aria-checked')===${JSON.stringify(String(enabled))}`,'Enable intent did not save')}
async function remove(){await ui("document.querySelector('.extension-details').open=true");await click('Remove')}

try {
 console.log('store phase: ready')
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));origin='https://localhost:'+server.address().port
 const fingerprint=new X509Certificate(fs.readFileSync(path.join(base,'cert.pem'))).fingerprint256
 session.fromPartition('amadeus-extension-downloads',{cache:false}).setCertificateVerifyProc((r,done)=>done(r.hostname==='localhost'&&new X509Certificate(r.certificate.data).fingerprint256===fingerprint?0:-3))
 const download=(url,target,limit,signal,progress)=>downloadExtension(url===OFFICIAL_EXTENSION_INDEX?origin+'/index.json':url.startsWith('https://artifact.test/')?origin+'/wheels/'+new URL(url).pathname.split('/').at(-1):url,target,limit,signal,progress)
 const catalog=new ExtensionCatalog(path.join(base,'catalog'),store,helper,installer,()=>String(store.snapshot({}).values.AMADEUS_EXTENSION_ARTIFACT_MIRROR||''),notify,download)
 const reply=async fn=>{try{return {ok:true,settings:await fn()}}catch(e){return {ok:false,error:e.message}}}
 const bind=(name,fn)=>ipcMain.handle(name,(event,...args)=>{assert.equal(event.sender,window.webContents);return fn(...args)})
 bind('store.catalog',request=>catalog.dispatch(request,{},async url=>{opened.push(url)}))
 bind('store.settings',()=>store.snapshot({}))
 bind('store.settings.update',update=>reply(async()=>{const value=store.update({},update);notify();return value}))
 bind('store.update',update=>reply(()=>installer.update(update)))
 bind('store.remove',(id,options)=>reply(()=>installer.remove(id,options)))
 bind('store.theme',()=>installer.themeState())
 const {createServer}=await import('vite')
 vite=await createServer({configFile:path.join(root,'electron/vite.config.ts'),configLoader:'runner',root:path.join(root,'electron'),cacheDir:path.join(base,'vite-cache'),server:{host:'127.0.0.1',port:0,strictPort:false,hmr:false},plugins:[{name:'real-store-fixture',configureServer(server){server.middlewares.use(async(request,response,next)=>{if(request.url?.split('?')[0]!=='/store.html')return next();response.setHeader('Content-Type','text/html');response.end(await server.transformIndexHtml('/store.html','<!doctype html><html><body><div id="root"></div><script type="module" src="/tests/fixtures/extensionSettings.tsx"></script></body></html>'))})}}]})
 await vite.listen();const url='http://127.0.0.1:'+vite.httpServer.address().port+'/store.html?real=1&presentation=plugins'
 window=new BrowserWindow({width:1120,height:1000,show:false,webPreferences:{preload:path.join(root,'electron/tests/fixtures/extensionStore.preload.cjs'),contextIsolation:true,nodeIntegration:false,backgroundThrottling:false,offscreen:true}})
 const externalRequests=[];window.webContents.session.webRequest.onBeforeRequest((details,callback)=>{if(!details.url.startsWith('http://127.0.0.1:')&&!details.url.startsWith('ws://127.0.0.1:')&&!details.url.startsWith('data:')&&!details.url.startsWith('blob:'))externalRequests.push(details.url);callback({})})
 console.log('store phase: loading renderer')
 window.webContents.on('console-message',event=>{if(event.level==='error')console.log('renderer:',event.message)})
 window.webContents.on('preload-error',(_event,_file,error)=>console.log('preload:',error.message))
 await bounded(window.loadURL(url),'Renderer load timed out');console.log('store phase: renderer loaded');await until("document.body.innerText.includes('No community extensions installed.')",'Manager did not load')
 check('opening installed does not fetch remote catalog',network.length===0)
 await click('Browse');await until("document.body.innerText.includes('Quiet desktop')",'Browse did not load')
 await input('input[type=search]',themeId);await click('View plugin details')
 check('third-party descriptions are plain text',await ui(`document.body.innerText.includes(${JSON.stringify(malicious)})&&!document.querySelector('.extension-catalog img')&&!globalThis.catalogInjected`))
 await click('Open homepage');await until("document.querySelector('[role=alert]')",'Unsafe homepage was not rejected');check('unsafe homepage rejected in production dispatcher',opened.length===0)
 await click('Open source repository');check('HTTPS source link resolved in main',opened[0]==='https://example.test/source')
 await capture('store-details-en')
 await click('Install plugin…');await until("document.querySelector('progress')?.value>0",'Download progress not shown')
 await click('Installed');check('switching views retains active download',await ui("Boolean(document.querySelector('.extension-download-progress'))"))
 await capture('store-download-en')
 await click('Cancel installation');await until("document.body.innerText.includes('No new package was saved.')",'Cancellation not acknowledged')
 check('cancellation leaves no saved install',store.storedExtensionRecords().length===0)
 await click('Browse');await until("!document.querySelector('.extension-catalog-toolbar button').disabled",'Refresh not finished')
 const before=confirms
 await ui("(()=>{const b=Array.from(document.querySelectorAll('button')).find(n=>n.getClientRects().length&&n.textContent==='Install plugin…');b.click();b.click()})()")
 await installed(themeId,'0.1.0');check('double click commits one install',confirms===before+1)
 check('new install stays disabled',store.storedExtensionRecords()[0].enabled===false)
 await configure(true)
 await click('Open appearance settings');await until("Array.from(document.querySelectorAll('[role=radio]')).some(n=>n.textContent.includes('Quiet day'))",'Installed theme absent in Appearance')
 await ui("Array.from(document.querySelectorAll('[role=radio]')).find(n=>n.textContent.includes('Quiet day')).click()")
 await until("document.documentElement.dataset.themeExtension==='com.example.desktop-theme/day'",'Real installed theme did not paint')
 check('real theme applies through Appearance',(await installer.themeState()).theme.version==='0.1.0')
 await click('Manage plugins');await until("document.body.innerText.includes('Quiet desktop')",'Manager return failed')
 index={...index,sequence:2,entries:structuredClone(fixture.index.entries)}
 const future=structuredClone(index.entries.find(e=>e.id===themeId&&e.version==='0.2.0'));future.version='0.10.0';future.compatibility.contributions.push({kind:'future',role:'',contract:'future/v1'});index.entries.push(future)
 await click('Check updates and security status');await until("document.body.innerText.includes('Compatible update available')",'Update recommendation absent')
 check('newer incompatible version is skipped',await ui("document.body.innerText.includes('0.1.0 → 0.2.0')&&!document.body.innerText.includes('0.1.0 → 0.10.0')"))
 await click('Review update details')
 await ui("(()=>{const n=document.querySelector('.extension-catalog-detail select');n.value='0.10.0';n.dispatchEvent(new Event('change',{bubbles:true}))})()")
 await until("document.body.innerText.includes('unsupported contribution contract')",'Future contract explanation missing')
 check('future mixed package details remain visible but installation is disabled',await ui("Array.from(document.querySelectorAll('button')).some(n=>n.textContent==='Install selected version…'&&n.disabled)"))
 const futureResult=await ui("window.amadeus.extensionCatalog({action:'install',id:'com.example.desktop-theme',version:'0.10.0'})")
 check('main rejects forced future-version installation',futureResult.ok===false)
 await capture('store-future-version-en')
 await ui("document.querySelector('#extension-tab-browse').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}))")
 check('keyboard switches manager views',await ui("document.querySelector('#extension-tab-installed').getAttribute('aria-selected')==='true'"))
 store.update({}, {values:{AMADEUS_EXTENSION_ARTIFACT_MIRROR:origin+'/mirror'}})
 badWheel=true;slow=false
 await click('Update plugin…');await until("document.querySelector('[role=alert]')&&!document.querySelector('.extension-download-progress')",'Bad update did not fail')
 check('failed update preserves saved and applied old theme',store.storedExtensionRecords()[0].version==='0.1.0'&&(await installer.themeState()).theme.version==='0.1.0')
 badWheel=false;await click('Update plugin…');await installed(themeId,'0.2.0')
 check('manual update preserves enable intent and applies resource package',store.storedExtensionRecords()[0].enabled&&(await installer.themeState()).theme.version==='0.2.0')
 await capture('store-updated-en')
 // Offline refresh preserves the installed version and enabled state.
 offline=true;await click('Check updates and security status');await until("document.querySelector('[role=alert]')",'Offline refresh failure absent')
 check('offline refresh preserves saved version and enable intent',store.storedExtensionRecords()[0].version==='0.2.0'&&store.storedExtensionRecords()[0].enabled)
 offline=false;await configure(false)
 check('disable restores builtin appearance',!(await installer.themeState()).theme)
 const data=path.join(base,'extension-data',createHash('sha256').update(themeId).digest('hex'));fs.writeFileSync(path.join(data,'retained.txt'),'fixture')
 await remove();await click('Confirm remove');await until("document.body.innerText.includes('No community extensions installed.')",'Uninstall failed')
 check('uninstall retains data by default',fs.existsSync(path.join(data,'retained.txt')))
 await click('Browse');await input('input[type=search]',themeId);await click('View plugin details');await click('Install plugin…');await installed(themeId,'0.2.0')
 await remove();await ui("Array.from(document.querySelectorAll('input[type=checkbox]')).find(n=>n.closest('.extension-removal-data')).click()");await click('Confirm remove');await until("document.body.innerText.includes('No community extensions installed.')",'Explicit deletion failed')
 check('explicit uninstall data deletion',!fs.existsSync(data))
 // A process package uses the same UI and real offline pip transaction, then its actual Runner.
 index={...index,sequence:3}
 await click('Browse');await input('input[type=search]',toneId);await click('View plugin details');await ui("(()=>{const n=document.querySelector('.extension-catalog-detail select');n.value='0.1.0';n.dispatchEvent(new Event('change',{bubbles:true}))})()");await click('Install plugin…');await installed(toneId,'0.1.0');await configure(true)
 const record=JSON.parse(await installer.launchSnapshot()).find(r=>r.id===toneId);assert.ok(record&&!record.startup_error)
 fs.writeFileSync(path.join(base,'tone-record.json'),JSON.stringify(record))
 const tone=fixture.index.entries.find(e=>e.id===toneId&&e.version==='0.1.0')
 await new Promise((resolve,reject)=>{const child=spawn(python,['-I',path.join(root,'tools/check_extension_contract.py'),'--execute','--kind','tts','--wheel',fixture.wheels[new URL(tone.url).pathname.split('/').at(-1)],'--record',path.join(base,'tone-record.json'),'--scenario',path.join(root,'examples/extensions/contract-scenarios/tone.json'),'--output',path.join(base,'tone-runtime.json')],{cwd:root,env:environment,windowsHide:true,stdio:'ignore'});child.on('error',reject);child.on('close',code=>code===0?resolve():reject(new Error('Tone contract failed')))})
 check('UI-installed TTS runs through the actual Runner',JSON.parse(fs.readFileSync(path.join(base,'tone-runtime.json'))).ok)
 await configure(false);await remove();await click('Confirm remove');await installer.cleanupStoppedBackend()
 index={...index,sequence:4,security:[{id:themeId,version:'0.2.0',sha256:fixture.index.entries.find(e=>e.id===themeId&&e.version==='0.2.0').sha256,status:'blocked',sequence:4,reason:'security',evidence:'https://fixture.test/block'}]}
 index.entries.find(e=>e.id===themeId&&e.version==='0.1.0').withdrawal={reason:'copyright',evidence:'https://fixture.test/copyright'}
 store.update({}, {values:{AMADEUS_UI_LOCALE:'zh-CN',AMADEUS_UI_THEME:'wallpaper-slice'}})
 await window.loadURL(url+'&locale=zh-CN&theme=wallpaper-slice');window.setSize(560,950)
 await until("document.body.innerText.includes('浏览')",'Chinese manager missing');await click('浏览');await input('input[type=search]',themeId);await click('查看插件详情');await ui("(()=>{const n=document.querySelector('.extension-catalog-detail select');n.value='0.2.0';n.dispatchEvent(new Event('change',{bubbles:true}))})()");await pause(80)
 check('Chinese security block is visible',await ui("document.body.innerText.includes('安全原因被封禁')"))
 check('blocked install control is disabled',await ui("Array.from(document.querySelectorAll('button')).some(n=>n.textContent==='安装插件…'&&n.disabled)"))
 await ui("(()=>{const n=document.querySelector('.extension-catalog-detail select');n.value='0.1.0';n.dispatchEvent(new Event('change',{bubbles:true}))})()")
 await until("document.body.innerText.includes('因版权原因下架')",'Copyright distinction missing')
 check('copyright is distinguished from security ban',true)
 check('narrow store does not overflow',await ui('document.documentElement.scrollWidth<=innerWidth'))
 await capture('store-withdrawal-zh-narrow')
 check('third-party catalog never requests remote images',externalRequests.length===0)
 check('renderer has no unhandled errors',(await ui('window.extensionFixture.errors')).length===0)
 check('staging directories are cleaned',fs.readdirSync(path.join(base,'catalog/staging')).length===0)
 report.ok=true
} catch(error){report.error=String(error.stack||error);if(window){report.rendererErrors=await ui('window.extensionFixture?.errors').catch(()=>[]);await bounded(capture('failure'),'Capture timed out').catch(()=>{})}console.error(error)}
finally{fs.writeFileSync(path.join(base,'report.json'),JSON.stringify(report,null,2)+'\n');if(window)window.destroy();await vite?.close();await session.fromPartition('amadeus-extension-downloads').closeAllConnections();server.close();console.log(JSON.stringify({ok:report.ok,checks:report.checks.length,report:path.join(base,'report.json')}));app.exit(report.ok?0:1)}

}).catch(error=>{console.error(error);app.exit(1)})
