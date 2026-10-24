// Loaded only through the owned smoke process inspector; never shipped.
async function setupReleaseFixture(base, automaticConfirm = true) {
  const require = process.getBuiltinModule('module').createRequire(process.execPath)
  const {app, net, session, dialog} = require('electron')
  const fs = require('node:fs'), path = require('node:path')
  const {spawn} = require('node:child_process')
  const {X509Certificate} = require('node:crypto')
  const fixture = JSON.parse(fs.readFileSync(path.join(base, 'fixture.json')))
  const state = globalThis.extensionReleaseFixture = {fixture, requests:[], confirmations:0, offline:false, badWheel:false, holdWheel:false,
    index:{...fixture.index, entries:fixture.index.entries.filter(e=>e.version==='0.1.0')}}
  // Keep the TLS server out of the inspected packaged main process, where the
  // in-process fixture exhibited native crashes and intermittent TLS failures.
  // The actual Electron net client and its certificate verification still run.
  const server = spawn(process.execPath, ['-e', `
    const fs=require('node:fs'),https=require('node:https');
    const base=${JSON.stringify(base)},path=require('node:path');
    let next=0;const pending=new Map();
    const server=https.createServer({key:fs.readFileSync(path.join(base,'key.pem')),cert:fs.readFileSync(path.join(base,'cert.pem'))},(req,res)=>{
      const id=++next;pending.set(id,res);process.send({id,url:req.url});
      res.on('close',()=>pending.delete(id));
    });
    process.on('message',s=>{
      const res=pending.get(s.id);if(!res)return;
      if(s.offline){res.writeHead(503);res.end();return}
      if(s.index){res.end(JSON.stringify(s.index));return}
      if(!s.wheel){res.writeHead(404);res.end();return}
      const bytes=fs.readFileSync(s.wheel);if(s.badWheel)bytes[0]^=255;
      res.writeHead(200,{'Content-Length':bytes.length});
      if(s.holdWheel){res.write(bytes.subarray(0,Math.max(1,Math.floor(bytes.length/2))));return}
      res.end(bytes);
    });
    process.on('disconnect',()=>{server.closeAllConnections();server.close(()=>process.exit(0))});
    server.listen(0,'127.0.0.1',()=>process.send({origin:'https://localhost:'+server.address().port}));
  `], {env:{SystemRoot:process.env.SystemRoot,WINDIR:process.env.WINDIR,ELECTRON_RUN_AS_NODE:'1'},windowsHide:true,stdio:['ignore','ignore','pipe','ipc']})
  server.stderr.on('data',data=>console.error('release fixture:',String(data)))
  const origin=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('Release fixture server did not start')),10000)
    server.once('error',error=>{clearTimeout(timer);reject(error)})
    server.once('exit',code=>{clearTimeout(timer);reject(Error('Release fixture server exited: '+code))})
    server.on('message',request=>{
      if(request.origin){clearTimeout(timer);resolve(request.origin);return}
      state.requests.push(request.url)
      server.send({id:request.id,offline:state.offline,badWheel:state.badWheel,holdWheel:state.holdWheel,
        index:request.url==='/index.json'?state.index:undefined,
        wheel:fixture.wheels[decodeURIComponent(request.url.split('/').at(-1))]})
    })
  })
  app.once('will-quit',()=>{if(server.connected)server.disconnect()})
  const fingerprint=new X509Certificate(fs.readFileSync(path.join(base,'cert.pem'))).fingerprint256
  session.fromPartition('amadeus-extension-downloads',{cache:false}).setCertificateVerifyProc((r,done)=>done(r.hostname==='localhost'&&new X509Certificate(r.certificate.data).fingerprint256===fingerprint?0:-3))
  const request=net.request.bind(net)
  net.request=options=>{
    const url=typeof options==='string'?options:options.url
    const target=url==='https://raw.githubusercontent.com/Code-Amadeus/Amadeus-Extensions/main/catalog/v1/index.json'?origin+'/index.json'
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
