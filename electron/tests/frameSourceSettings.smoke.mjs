// Optional asset-free GUI inspection of the real Graphics settings components.
import { app, BrowserWindow } from 'electron';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const output = path.join(root, 'output/diagnostics/frame-source-settings-smoke');
app.setPath('userData', path.join(output, 'profile'));
async function main() {
  const { createServer } = await import('vite');
  await fs.mkdir(output, { recursive: true });
  await app.whenReady();
  const seed = { page: 'settings', section: 'graphics', locale: 'zh-CN', capabilities: { expressions: [], lip_sync_ids: [], warnings: [] },
    systemConfig: { graphics: { profile: 'standard', custom_max_fps: 30, custom_max_resolution: 1.5,
      texture_sampling: true, bc7_cache: true, frame_source: 'auto', frame_budget_mib: 'auto',
      effective_max_fps: 60, effective_max_resolution: null } },
    surfaces: { wallpaper: { state: 'ready', runtime_id: 'fixture', revision: 1, profile_id: '',
      diagnostic: { frame_source: { mode: 'static', reason: 'video_pack_missing', budgetMiB: 2048 } } } } };
  const server = await createServer({ root: path.join(root, 'electron'), configFile: path.join(root, 'electron/vite.config.ts'),
    configLoader: 'runner', server: { host: '127.0.0.1', port: 0 }, plugins: [{ name: 'frame-source-settings-smoke',
      configureServer(server) { server.middlewares.use(async (req,res,next) => {
        if (req.url !== '/frame-settings.html') return next();
        const html = '<html><head><meta charset="utf-8"></head><body><div id="root"></div><script>window.visualSeed='
          + JSON.stringify(seed) + '</script><script type="module" src="/tests/fixtures/characterVisuals.tsx"></script></body></html>';
        res.setHeader('Content-Type','text/html; charset=utf-8');
        res.end(await server.transformIndexHtml('/frame-settings.html', html));
      }); } }] });
  await server.listen();
  const window = new BrowserWindow({ show: false, width: 1250, height: 1200,
    webPreferences: { contextIsolation: true, nodeIntegration: false, offscreen: true } });
  try {
    await window.loadURL(`http://127.0.0.1:${server.httpServer.address().port}/frame-settings.html`);
    const run = script => window.webContents.executeJavaScript(script);
    const until = async (script) => { const end=Date.now()+15000; while(Date.now()<end) {
      if(await run(script)) return; await new Promise(r=>setTimeout(r,100)); } throw Error('GUI state did not appear: '+script); };
    await until('document.body.textContent.includes("2048 MiB") && document.body.textContent.includes("帧源选择")');
    const choices = await run('[...document.querySelectorAll("select")].map(s=>[...s.options].map(o=>({value:o.value,label:o.textContent})))');
    assert.ok(choices.some(items=>items.some(o=>o.value==='auto')&&items.some(o=>o.value==='static')));
    assert.ok(choices.some(items=>items.some(o=>o.value==='2048')&&items.some(o=>o.value==='384')));
    assert.ok(choices.some(items=>items.some(o=>o.value==='standard')&&items.some(o=>o.value==='power_saving')&&items.some(o=>o.value==='custom')));
    await fs.writeFile(path.join(output,'static-default.png'),(await window.webContents.capturePage()).toPNG());
    await run(`window.visualFixture.emit('visual.updated', {surfaces:{wallpaper:{state:'ready',diagnostic:{frame_source:{mode:'video',reason:'video_ready',budgetMiB:384}}}}})`);
    await until('document.querySelector("[role=status]").textContent.includes("384 MiB")');
    await new Promise(r=>setTimeout(r,250));
    await fs.writeFile(path.join(output,'video-active.png'),(await window.webContents.capturePage()).toPNG());
    await run(`window.visualFixture.emit('visual.updated', {surfaces:{wallpaper:{state:'ready',diagnostic:{frame_source:{mode:'static',reason:'video_unavailable',budgetMiB:2048}}}}})`);
    await until('document.querySelector("[role=status]").textContent.includes("2048 MiB")');
    assert.deepEqual(await run('window.visualFixture.errors'), []);
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify({passed:true,checks:['existing presets retained','auto/static and budget controls','old-pack status','live video status','fallback status'],choices},null,2));
    console.log(JSON.stringify({passed:true,output}));
  } finally { window.destroy(); await server.close(); }
}
main().then(()=>app.exit(0),error=>{console.error(error);app.exit(1)});
