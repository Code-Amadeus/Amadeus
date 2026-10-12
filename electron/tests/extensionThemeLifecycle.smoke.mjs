// Run via tools/probes/extension_theme_lifecycle.py; uses real owners with synthetic packages.
import {app, BrowserWindow} from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import assert from 'node:assert/strict'
import {createHash} from 'node:crypto'
import {spawn} from 'node:child_process'
import {createInterface} from 'node:readline'
import {fileURLToPath} from 'node:url'
import {ExtensionInstaller,ExtensionPackageHelper,extensionInstallerEnvironment,runExtensionCommand} from '../../electron/dist/main/extensions.js'
import {DesktopSettingsStore} from '../../electron/dist/main/desktopSettings.js'
import {ExtensionThemeResources} from '../../electron/dist/main/extensionThemes.js'
import {forceStopBackendTree} from '../../electron/dist/main/backendProcessTree.js'
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..')
const base=process.env.AMADEUS_THEME_PROBE_DIR
if (!base || !path.isAbsolute(base)) throw new Error('Use the Python lifecycle probe launcher')
async function bounded(promise, milliseconds, label) {
  let timer
  try { return await Promise.race([promise, new Promise((_, reject) => { timer=setTimeout(()=>reject(new Error(label+' timed out')),milliseconds) })]) }
  finally { clearTimeout(timer) }
}
async function run(){
const profile=fs.mkdtempSync(path.join(base,'owner-profile-'))
app.setPath('userData',path.join(profile,'electron'))
await app.whenReady()
const python=process.env.AMADEUS_PYTHON
const environment={...extensionInstallerEnvironment(process.env),PIP_NO_INDEX:'1',PIP_FIND_LINKS:path.join(root,'vendor/extension-sdk')}
const helper=new ExtensionPackageHelper(root,()=>python,environment)
const store=new DesktopSettingsStore(path.join(profile,'settings.json'),path.join(profile,'absent.env'),process.platform,helper.validateRecord)
const fontWindow=new BrowserWindow({show:false,webPreferences:{contextIsolation:true}})
fs.writeFileSync(path.join(profile,'fonts.html'),'<html><body>Font probe</body></html>')
await fontWindow.loadFile(path.join(profile,'fonts.html'))
const fonts=await bounded(fontWindow.webContents.executeJavaScript("(async()=>[...new Set((await queryLocalFonts()).map(f=>f.family))])()",true),15000,'Installed-font query')
assert.ok(fonts.includes('Segoe UI'))
let confirm=true
const reviews=[]
const preparations={venv:0,pip:0}
const command=async (executable,args,options)=>{
  if (args.includes('venv')) preparations.venv++
  if (args.includes('pip')) preparations.pip++
  return runExtensionCommand(executable,args,options)
}
const installer=new ExtensionInstaller(path.join(profile,'extensions'),root,store,helper,()=>python,environment,command,async value=>{reviews.push({operation:value.operation,accepted:confirm});return confirm},undefined,new ExtensionThemeResources(async()=>fonts))
const wheel=(name,version)=>path.join(base,'wheels',name+'-'+version+'-py3-none-any.whl')
const report={passed:false,checks:[],backendPids:[],reviews,preparations,fontsVerified:true,nativeDialogs:false}
let backend
async function start(){
  const snapshot=await installer.prepareLaunch()
  const child=spawn(python,['-u',path.join(root,'tests/fixtures/extension_theme_runtime.py')],{cwd:root,env:environment,windowsHide:true,stdio:['pipe','pipe','pipe']})
  child.stderr.on('data',chunk=>fs.appendFileSync(path.join(base,'runtime.stderr.log'),chunk))
  const closed=new Promise((resolve,reject)=>{child.once('close',resolve);child.once('error',reject)})
  closed.catch(()=>{})
  backend={child,closed,lines:createInterface({input:child.stdout})[Symbol.asyncIterator]()}
  child.stdin.on('error',()=>{})
  child.stdin.write(snapshot.extensions+'\n')
  const ready=await response()
  backend.pid=ready.pid
  assert.ok(await installer.acceptLaunch(snapshot,ready.accepted))
  report.backendPids.push(ready.pid)
}
async function response(){
  const line=await bounded(backend.lines.next(),30000,'Runtime response')
  assert.equal(line.done,false,'Runtime exited before replying; inspect runtime.stderr.log')
  const result=JSON.parse(line.value)
  assert.equal(result.error,undefined,result.error)
  return result
}
async function speak(){
  backend.child.stdin.write(JSON.stringify({speak:'org.example.gui-mixed-theme/tone'})+'\n')
  const result=await response()
  assert.equal(result.samples,4800);assert.equal(result.rate,24000);return result
}
async function stop(){
  if(!backend)return
  const previous=backend
  backend=undefined
  previous.child.stdin.end('{"stop":true}\n')
  let code
  try { code=await bounded(previous.closed,20000,'Runtime shutdown') }
  catch(error){await forceStopBackendTree(previous.child);throw error}
  await installer.cleanupStoppedBackend()
  assert.equal(code,0,'Runtime must exit cleanly')
}
const selected=id=>store.update(environment,{values:{AMADEUS_UI_THEME:id}})
const record=id=>store.storedExtensionRecords().find(r=>r.id===id)
const pure='com.example.desktop-theme',mixed='org.example.gui-mixed-theme'
try{
  await start();const purePid=backend.pid
  await installer.install(wheel('amadeus_example_theme','0.1.0'))
  assert.equal(record(pure).sitePackages,'');assert.equal(record(pure).enabled,false)
  await installer.update({id:pure,enabled:true});selected(pure+'/night')
  assert.equal((await installer.themeState()).theme.image.mime,'image/png')
  const old=record(pure),data=path.join(profile,'extension-data',createHash('sha256').update(pure).digest('hex'))
  fs.writeFileSync(path.join(data,'retained.txt'),'synthetic')
  confirm=false;await assert.rejects(installer.install(wheel('amadeus_example_theme','0.2.0')),/cancelled/);confirm=true
  assert.equal(record(pure).revision,old.revision)
  await installer.install(wheel('amadeus_example_theme','0.2.0'))
  assert.equal((await installer.themeState()).theme.version,'0.2.0')
  assert.equal(fs.existsSync(path.join(profile,'extensions',old.installId)),false)
  await assert.rejects(installer.install(wheel('amadeus_example_theme','0.3.0')),/contrast/)
  assert.equal(record(pure).version,'0.2.0')
  await installer.update({id:pure,enabled:false});assert.equal((await installer.themeState()).theme,undefined)
  await installer.update({id:pure,enabled:true});assert.ok((await installer.themeState()).theme)
  await installer.remove(pure);assert.ok(fs.existsSync(path.join(data,'retained.txt')))
  assert.equal((await installer.themeState()).selected,pure+'/night')
  await installer.install(wheel('amadeus_example_theme','0.2.0'));assert.equal(record(pure).enabled,false)
  await installer.remove(pure,{deleteData:true});assert.equal(fs.existsSync(data),false)
  assert.equal(backend.pid,purePid);assert.equal(backend.child.exitCode,null)
  assert.deepEqual(preparations,{venv:0,pip:0})
  report.checks.push('Real resource wheel install/confirm decision/enable/image/select/cancel/update/invalid update/disable/re-enable/remove/reinstall/data deletion; no pip/venv; backend PID unchanged')
  await installer.install(wheel('amadeus_gui_tone','0.1.0'))
  await installer.update({id:mixed,enabled:true});selected(mixed+'/mixed')
  assert.equal((await installer.themeState()).theme,undefined)
  await stop();await start();assert.equal((await installer.themeState()).theme.version,'0.1.0');await speak()
  const oldMixed=record(mixed),v1Pid=backend.pid
  await installer.install(wheel('amadeus_gui_tone','0.2.0'))
  assert.equal((await installer.themeState()).theme.version,'0.1.0')
  assert.ok(fs.existsSync(path.join(profile,'extensions',oldMixed.installId)))
  await speak();assert.equal(backend.pid,v1Pid)
  await installer.update({id:mixed,enabled:false});assert.equal((await installer.themeState()).theme,undefined)
  await installer.update({id:mixed,enabled:true});assert.equal((await installer.themeState()).theme,undefined)
  await stop();await start()
  assert.notEqual(backend.pid,v1Pid);assert.equal((await installer.themeState()).theme.version,'0.2.0')
  assert.equal(fs.existsSync(path.join(profile,'extensions',oldMixed.installId)),false)
  await speak()
  const mixedData=path.join(profile,'extension-data',createHash('sha256').update(mixed).digest('hex'))
  const events=fs.readFileSync(path.join(mixedData,'lifecycle.jsonl'),'utf8').trim().split('\n').map(JSON.parse)
  assert.deepEqual(events.filter(e=>e.event==='completed').map(e=>e.version),['0.1.0','0.1.0','0.2.0'])
  report.runnerPids=[...new Set(events.filter(e=>e.event==='initialize').map(e=>e.pid))]
  await installer.remove(mixed,{deleteData:true});assert.equal((await installer.themeState()).theme,undefined)
  await stop();assert.equal(record(mixed),undefined);assert.equal(fs.existsSync(mixedData),false)
  report.checks.push('Real mixed package private venv and SDK Runner: v1 synthesis before/after saved v2; frontend admission closes immediately; new backend and Runner apply v2 together; removal cleans after stop')
  report.passed=true
}catch(error){report.error=String(error.stack||error)}
finally{
  try { await stop() } catch(error){report.passed=false;report.cleanupError=String(error)}
  fontWindow.destroy()
  fs.writeFileSync(path.join(base,'report.json'),JSON.stringify(report,null,2))
  app.exit(report.passed?0:1)
}

}
run().catch(error=>{fs.writeFileSync(path.join(base,'report.json'),JSON.stringify({passed:false,error:String(error.stack||error)}));app.exit(1)})
