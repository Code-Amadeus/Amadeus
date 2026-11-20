/** Real Electron net + real wheels + real installer/settings; explicit fixture authority only. */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import https from 'node:https'
import http from 'node:http'
import tcp from 'node:net'
import { X509Certificate, createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { app, session } from 'electron'
import { DesktopSettingsStore } from '../dist/main/desktopSettings.js'
import { ExtensionInstaller, ExtensionPackageHelper } from '../dist/main/extensions.js'
import { ExtensionCatalog, OFFICIAL_EXTENSION_INDEX } from '../dist/main/extensionCatalog.js'
import { downloadExtension, MAX_INDEX_BYTES } from '../dist/main/extensionDownloads.js'
const root = fileURLToPath(new URL('../../', import.meta.url))
const base = path.resolve(process.env.AMADEUS_CATALOG_JOURNEY_ROOT)
const relative = path.relative(path.join(root, 'build/extension-catalog-journey'), base)
assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative))
const python = process.env.AMADEUS_TEST_PYTHON
const fixture = JSON.parse(fs.readFileSync(path.join(base, 'fixture.json')))
let index = structuredClone(fixture.index), badMirror = false, banOnConfirm = false
const report = process.argv.includes('--recover') ? JSON.parse(fs.readFileSync(path.join(base, 'report.json'))) : { ok: false, checks: {}, artifacts: fixture.index.entries.map(e => ({ id:e.id,version:e.version,sha256:e.sha256 })), fixtureAuthority: true, nativeDialogs: false, osTrustModified: false }
const checked = (name, value = true) => { assert.ok(value, name); report.checks[name] = true }
const requests = []
let origin, catalog
const environment = { ...process.env, PIP_CONFIG_FILE: process.platform === 'win32' ? 'NUL' : '/dev/null' }
const helper = new ExtensionPackageHelper(root, () => python, environment)
const settingsFile = path.join(base, 'settings.json')
const store = new DesktopSettingsStore(settingsFile, path.join(base, '.env'), process.platform, helper.validateRecord)
const installer = new ExtensionInstaller(path.join(base, 'extensions'), root, store, helper, () => python, environment, undefined, async review => {
  if (banOnConfirm) {
    index.sequence++
    index.security = [{ id:review.manifest.id,version:review.manifest.version,sha256:review.digest,status:'blocked',sequence:index.sequence,reason:'security',evidence:'https://fixture.test/ban' }]
    await catalog.refresh()
  }
  return true
})
const server = https.createServer({ key:fs.readFileSync(path.join(base,'key.pem')),cert:fs.readFileSync(path.join(base,'cert.pem')) }, (request,response) => {
  requests.push({ url:request.url,cookie:request.headers.cookie,authorization:request.headers.authorization })
  const url = new URL(request.url, origin)
  if (url.pathname === '/index.json') { response.end(JSON.stringify(index)); return }
  if (url.pathname === '/redirect') { response.writeHead(302,{Location:origin+'/bytes'});response.end();return }
  if (url.pathname === '/downgrade') { response.writeHead(302,{Location:'http://localhost/unsafe'});response.end();return }
  if (url.pathname === '/auth') { response.writeHead(401,{'WWW-Authenticate':'Basic realm="fixture"'});response.end();return }
  if (url.pathname === '/oversize') { response.end(Buffer.alloc(100));return }
  if (url.pathname === '/slow') { response.write(Buffer.alloc(10));setTimeout(()=>response.end(Buffer.alloc(10)),1500);return }
  if (url.pathname === '/bytes') { response.end('fixture');return }
  const wheel = fixture.wheels[decodeURIComponent(url.pathname.split('/').at(-1))]
  if (wheel) { const bytes=fs.readFileSync(wheel);response.end(badMirror && url.pathname.startsWith('/mirror/') ? Buffer.alloc(bytes.length,1) : bytes);return }
  response.writeHead(404);response.end()
})
let tunnels = 0
const proxy = http.createServer()
proxy.on('connect',(request,socket,head)=> {
  const target = new URL('https://'+request.url)
  if (target.hostname !== 'localhost') { socket.destroy();return }
  tunnels++
  const upstream=tcp.connect(Number(target.port),'127.0.0.1',()=>{socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');if(head.length)upstream.write(head);socket.pipe(upstream);upstream.pipe(socket)})
  upstream.on('error',()=>socket.destroy());socket.on('error',()=>upstream.destroy())
})
app.setPath('userData',path.join(base,'profile'))
process.on('uncaughtException', error => { console.error(error); app.exit(1) })
app.whenReady().then(async()=> {
 try {
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));origin='https://localhost:'+server.address().port
  const isolated=session.fromPartition('amadeus-extension-downloads',{cache:false})
  const download = async(url,target,limit,signal,progress)=> {
    const actual = url === OFFICIAL_EXTENSION_INDEX ? origin+'/index.json' : url.startsWith('https://artifact.test/') ? origin+'/wheels/'+new URL(url).pathname.split('/').at(-1) : url
    await downloadExtension(actual,target,limit,signal,progress)
  }
  catalog = new ExtensionCatalog(path.join(base,'catalog'),store,helper,installer,()=>String(store.snapshot({}).values.AMADEUS_EXTENSION_ARTIFACT_MIRROR||''),()=>{},download)
  await installer.refreshValidation()
  const fingerprint=new X509Certificate(fs.readFileSync(path.join(base,'cert.pem'))).fingerprint256
  if (!process.argv.includes('--recover')) {
    const publicUrl='https://raw.githubusercontent.com/Code-Amadeus/Amadeus-Extensions/bf1a848448a19ed20eca029e8cc3991563e42e95/LICENSE'
    checked('systemProxyInherited',(await isolated.resolveProxy(publicUrl)) === (await session.defaultSession.resolveProxy(publicUrl)))
    await downloadExtension(publicUrl,path.join(base,'public-license.txt'),MAX_INDEX_BYTES,new AbortController().signal)
    checked('publicHttpsSystemCertificateValidation')
    await assert.rejects(downloadExtension(origin.replace('localhost','127.0.0.1')+'/bytes',path.join(base,'invalid-cert.bin'),100,new AbortController().signal))
    checked('invalidCertificateRejected',!fs.existsSync(path.join(base,'invalid-cert.bin')))
  }
  // Test-only trust of exactly one disposable certificate. No OS roots or production options change.
  isolated.setCertificateVerifyProc((request,callback)=> {
    const matches = request.hostname === 'localhost' && new X509Certificate(request.certificate.data).fingerprint256 === fingerprint
    callback(matches ? 0 : -3)
  })
  await isolated.closeAllConnections()
  if (process.argv.includes('--recover')) {
    checked('faultSurvivesNewElectronProcess',store.extensionSecurityState().status==='unavailable')
    store.update({}, { values:{AMADEUS_EXTENSION_ARTIFACT_MIRROR:origin+'/mirror'} })
    checked('faultSurvivesOrdinarySave',store.extensionSecurityState().status==='unavailable')
    index=JSON.parse(fs.readFileSync(path.join(base,'recovery-index.json')))
    await catalog.refresh(true)
    checked('explicitOfficialRecovery',store.extensionSecurityState().status==='ready')
    for(const record of store.storedExtensionRecords()) {
      await installer.update({id:record.id,enabled:false});await installer.remove(record.id)
    }
    await installer.cleanupStoppedBackend()
    report.ok=true
  } else {
    await isolated.cookies.set({url:origin,name:'fixture-session',value:'private-fixture',secure:true})
    await session.defaultSession.cookies.set({url:origin,name:'app-session',value:'private-fixture',secure:true})
    await downloadExtension(origin+'/redirect',path.join(base,'redirect.bin'),100,new AbortController().signal)
    checked('httpsRedirect')
    await assert.rejects(downloadExtension(origin+'/downgrade',path.join(base,'downgrade.bin'),100,new AbortController().signal))
    checked('downgradeRejected')
    await assert.rejects(downloadExtension(origin+'/auth',path.join(base,'auth.bin'),100,new AbortController().signal))
    checked('originAuthenticationNotUsed')
    await assert.rejects(downloadExtension(origin+'/oversize',path.join(base,'oversize.bin'),20,new AbortController().signal))
    checked('oversizeCleaned',!fs.existsSync(path.join(base,'oversize.bin')))
    const abort=new AbortController();setTimeout(()=>abort.abort(),100)
    await assert.rejects(downloadExtension(origin+'/slow',path.join(base,'cancelled.bin'),100,abort.signal))
    checked('cancelledCleaned',!fs.existsSync(path.join(base,'cancelled.bin')))
    await new Promise(resolve=>proxy.listen(0,'127.0.0.1',resolve))
    await isolated.setProxy({proxyRules:'http=127.0.0.1:'+proxy.address().port+';https=127.0.0.1:'+proxy.address().port,proxyBypassRules:'<-loopback>'})
    await isolated.closeAllConnections()
    await downloadExtension(origin+'/bytes',path.join(base,'proxy.bin'),100,new AbortController().signal)
    checked('chromiumHttpsConnectProxy',tunnels>0)
    await isolated.setProxy({mode:'system'});await isolated.closeAllConnections()
    const notes=fixture.index.entries.find(e=>e.id.endsWith('notes-mcp'))
    const theme=fixture.index.entries.find(e=>e.id.endsWith('desktop-theme'))
    const tone=fixture.index.entries.find(e=>e.id.endsWith('tone-tts'))
    const skill=fixture.index.entries.find(e=>e.id.endsWith('handoff-skill'))
    await catalog.install(notes.id,notes.version)
    checked('originalDownloadAndRealMcpInstall')
    store.update({}, {values:{AMADEUS_EXTENSION_ARTIFACT_MIRROR:origin+'/mirror'}})
    badMirror=true
    await assert.rejects(catalog.install(theme.id,theme.version))
    checked('mirrorWrongBytesRejected',!store.extensionIsInstalled(theme.id))
    badMirror=false
    await catalog.install(theme.id,theme.version)
    checked('mirrorRealThemeInstall')
    banOnConfirm=true
    await assert.rejects(catalog.install(tone.id,tone.version),/blocked/)
    banOnConfirm=false
    checked('confirmationBanBeforePip',!store.extensionIsInstalled(tone.id))
    await assert.rejects(installer.install(fixture.wheels[new URL(tone.url).pathname.split('/').at(-1)]),/blocked/)
    checked('sameBannedLocalWheelRejected')
    index.sequence++;index.security[0]={...index.security[0],status:'allowed',sequence:index.sequence,reason:'security-resolved'}
    await catalog.refresh();await catalog.install(tone.id,tone.version)
    checked('explicitHigherSequenceUnban')
    await catalog.install(skill.id,skill.version)
    for(const entry of fixture.index.entries) await installer.update({id:entry.id,enabled:true,...([notes.id,skill.id].includes(entry.id)?{providers:['pi']}:{})})
    const snapshots=JSON.parse(await installer.launchSnapshot())
    for(const snapshot of snapshots) { assert.ok(!snapshot.startup_error);fs.writeFileSync(path.join(base,snapshot.id+'.json'),JSON.stringify(snapshot)) }
    for (const [name, entry, kind] of [['tone',tone,'tts'],['notes',notes,'mcp'],['handoff',skill,'skill']]) {
      const recordFile=path.join(base,entry.id+'.json')
      const output=path.join(base,name+'-runtime.json')
      const args=['-I',path.join(root,'tools/check_extension_contract.py'),'--execute','--kind',kind,'--wheel',fixture.wheels[new URL(entry.url).pathname.split('/').at(-1)],'--record',recordFile,'--scenario',path.join(root,'examples/extensions/contract-scenarios',name+'.json'),'--output',output]
      await new Promise((resolve,reject)=> {
        const child=spawn(python,args,{cwd:root,env:environment,windowsHide:true,stdio:['ignore','pipe','pipe']})
        let outputText=''
        child.stdout.on('data',chunk=>{outputText+=chunk});child.stderr.on('data',()=>{})
        child.on('error',reject);child.on('close',code=>code===0?resolve():reject(new Error(name+' contract failed: '+outputText)))
      })
      checked(name+'RealRuntimeContract')
    }
    checked('fourRealReferencesEnabled',store.extensionRecords().length===4)
    checked('snapshotIdentitiesMatch',store.extensionRecords().every(r=>fixture.index.entries.some(e=>e.id===r.id&&e.version===r.version&&e.sha256===r.digest)))
    const view=await catalog.view();checked('reviewDerivedFromInstalledTriple',view.installed.every(r=>r.catalogState==='matched'))
    checked('requestCredentialsIsolated',requests.every(r=>!r.cookie&&!r.authorization))
    index.sequence++;index.security.push({id:notes.id,version:notes.version,sha256:notes.sha256,status:'blocked',sequence:index.sequence,reason:'security',evidence:'https://fixture.test/notes-ban'})
    await catalog.refresh()
    fs.unlinkSync(path.join(base,'catalog/index.json'))
    checked('cacheDeletionRetainsBan',Boolean(store.extensionSecurityBlock({id:notes.id,version:notes.version,digest:notes.sha256})))
    fs.writeFileSync(settingsFile,Buffer.alloc(fs.statSync(settingsFile).size))
    checked('nulPrimaryRecovered',store.extensionSecurityState().status==='ready')
    store.update({}, {values:{AMADEUS_EXTENSION_ARTIFACT_MIRROR:origin+'/mirror'}})
    for(const file of [settingsFile,settingsFile+'.bak'])fs.writeFileSync(file,Buffer.alloc(fs.statSync(file).size))
    checked('doubleNulFailsClosed',store.extensionSecurityState().status==='unavailable')
    await assert.rejects(installer.install(fixture.wheels[new URL(notes.url).pathname.split('/').at(-1)]),/damaged/)
    checked('damagedStoreBlocksLocalInstallation')
    fs.writeFileSync(path.join(base,'recovery-index.json'),JSON.stringify(index))
    checked('stagingClean',fs.readdirSync(path.join(base,'catalog/staging')).length===0)
  }
  fs.writeFileSync(path.join(base,'report.json'),JSON.stringify(report,null,2)+'\n')
  console.log(JSON.stringify({ok:true,phase:process.argv.includes('--recover')?'recover':'prepare',checks:Object.keys(report.checks).length}))
  await isolated.closeAllConnections();server.close();proxy.close();app.exit(0)
 } catch(error) {
  report.error=String(error);fs.writeFileSync(path.join(base,'report.json'),JSON.stringify(report,null,2)+'\n');console.error(error);app.exit(1)
 }
})
