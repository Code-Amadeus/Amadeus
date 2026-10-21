// Loaded only through the owned smoke process inspector; never shipped.
async function setupReleaseFixture(base, automaticConfirm = true) {
  const require = process.getBuiltinModule('module').createRequire(process.execPath)
  const {net, session, dialog} = require('electron')
  const fs = require('node:fs'), path = require('node:path'), https = require('node:https')
  const {X509Certificate} = require('node:crypto')
  const fixture = JSON.parse(fs.readFileSync(path.join(base, 'fixture.json')))
  const state = globalThis.extensionReleaseFixture = {fixture, requests:[], confirmations:0, offline:false, badWheel:false, holdWheel:false,
    index:{...fixture.index, entries:fixture.index.entries.filter(e=>e.version==='0.1.0')}}
  const server = https.createServer({key:fs.readFileSync(path.join(base,'key.pem')), cert:fs.readFileSync(path.join(base,'cert.pem'))}, (req,res)=>{
    state.requests.push(req.url)
    if(state.offline){res.writeHead(503);res.end();return}
    if(req.url==='/index.json'){res.end(JSON.stringify(state.index));return}
    const wheel=fixture.wheels[decodeURIComponent(req.url.split('/').at(-1))]
    if(!wheel){res.writeHead(404);res.end();return}
    const bytes=fs.readFileSync(wheel);if(state.badWheel)bytes[0]^=255;res.writeHead(200,{'Content-Length':bytes.length});if(state.holdWheel){res.write(bytes.subarray(0,Math.max(1,Math.floor(bytes.length/2))));return}res.end(bytes)
  })
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
  const origin='https://localhost:'+server.address().port
  const fingerprint=new X509Certificate(fs.readFileSync(path.join(base,'cert.pem'))).fingerprint256
  session.fromPartition('amadeus-extension-downloads',{cache:false}).setCertificateVerifyProc((r,done)=>done(r.hostname==='localhost'&&new X509Certificate(r.certificate.data).fingerprint256===fingerprint?0:-3))
  const request=net.request.bind(net)
  net.request=options=>{
    const url=typeof options==='string'?options:options.url
    const target=url==='https://raw.githubusercontent.com/Code-Amadeus/Amadeus-Extensions/main/catalog/index.json'?origin+'/index.json'
      : url.startsWith('https://artifact.test/')||url.startsWith('https://mirror.test/')?origin+new URL(url).pathname:null
    return request(target?{...options,url:target}:options)
  }
  const confirm=dialog.showMessageBox.bind(dialog)
  dialog.showMessageBox=async(...args)=>{
    const options=args.at(-1)
    if(options.title==='Review extension access'){state.confirmations++;state.lastConfirmation={message:options.message,detail:options.detail};if(automaticConfirm)return {response:1,checkboxChecked:false}}
    return confirm(...args)
  }
  return true
}
