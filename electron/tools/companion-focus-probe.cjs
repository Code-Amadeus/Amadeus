// An opt-in foreground editor used only by the isolated preview. This records
// focus/visibility; it never drives another application or reads microphone data.
const { BrowserWindow, screen } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

module.exports = async function focusProbe(palette, output) {
  // The native driver crops a spanning window to the primary screen but scales
  // input against its full bounds. Focus acceptance needs matching geometry;
  // the separate cross-display suite retains the full production surface.
  palette.setBounds(screen.getPrimaryDisplay().bounds);
  const editor = new BrowserWindow({ x:1700, y:100, width:500, height:310,
    title:'Amadeus · 输入焦点验收', autoHideMenuBar:true,
    webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true} });
  await editor.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(`<!doctype html><html lang="zh"><meta charset="utf-8">
    <style>body{background:#222;color:#eee;font:16px system-ui;padding:18px}textarea{width:95%;height:70px;background:#333;color:white}small{color:#bcb}</style>
    <h3>输入焦点验收 · 示例窗口</h3><textarea autofocus placeholder="此处应始终保留输入焦点"></textarea>
    <p id="state"></p><small>点击、拖动旁边的 Amadeus 卡片，检查此窗口是否失去焦点。</small>
    <script>window.probe={blur:0,focus:0,hidden:0};
    addEventListener('blur',()=>probe.blur++);addEventListener('focus',()=>probe.focus++);
    document.addEventListener('visibilitychange',()=>{if(document.hidden)probe.hidden++});
    setInterval(()=>state.textContent='失去焦点 '+probe.blur+' 次 · 不可见 '+probe.hidden+' 次',100);</script></html>`));
  editor.show(); editor.focus();
  palette.showInactive(); palette.moveTop();
  let mouseActivations=0;
  if(process.platform==='win32') palette.hookWindowMessage(0x21,()=>mouseActivations++);
  const timer=setInterval(async()=>{
    if(editor.isDestroyed()||palette.isDestroyed())return;
    const status=await editor.webContents.executeJavaScript('({...probe,hasFocus:document.hasFocus(),visibility:document.visibilityState})');
    fs.writeFileSync(path.join(output,'pointer-focus-probe.json'),JSON.stringify({at:new Date().toISOString(),...status,
      editorFocused:editor.isFocused(),paletteFocused:palette.isFocused(),paletteFocusable:palette.isFocusable(),mouseActivations},null,2));
  },300);
  const stop=()=>{clearInterval(timer);if(!editor.isDestroyed())editor.close()};
  palette.once('closed',stop);editor.once('closed',()=>clearInterval(timer));
};
