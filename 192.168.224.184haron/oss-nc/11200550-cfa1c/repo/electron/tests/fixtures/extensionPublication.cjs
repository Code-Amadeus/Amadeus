// Owned demo inspector only; no production URL or trust override is added.
async function setupPublicationTransport(origin, certificate) {
  const require = process.getBuiltinModule('module').createRequire(process.execPath)
  const {net, session, dialog} = require('electron')
  const fs = require('node:fs'), {X509Certificate} = require('node:crypto')
  const url = new URL(origin)
  if (url.protocol !== 'https:' || url.hostname !== 'localhost' || url.username || url.password)
    throw Error('Demo transport requires loopback HTTPS')
  const fingerprint = new X509Certificate(fs.readFileSync(certificate)).fingerprint256
  session.fromPartition('amadeus-extension-downloads', {cache:false})
    .setCertificateVerifyProc((r, done) => done(
      r.hostname === 'localhost' && new X509Certificate(r.certificate.data).fingerprint256 === fingerprint ? 0 : -3))
  const request = net.request.bind(net)
  const state = globalThis.extensionPublicationTransport = {requests:[], confirmations:0}
  net.request = options => {
    const source = typeof options === 'string' ? options : options.url
    const target = source === 'https://raw.githubusercontent.com/Code-Amadeus/Amadeus-Extensions/main/catalog/v1/index.json'
      ? origin + '/index.json' : source
    state.requests.push({source, target})
    return request(typeof options === 'string' ? target : {...options, url:target})
  }
  const confirm = dialog.showMessageBox.bind(dialog)
  dialog.showMessageBox = async (...args) => {
    if (args.at(-1).title === 'Review extension access') {
      state.confirmations++
      return {response:1, checkboxChecked:false}
    }
    return confirm(...args)
  }
  return true
}
