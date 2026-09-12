// Displays the production React component on the real secondary desktop.
const { app, BrowserWindow, screen, ipcMain, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '../..');
const output = path.join(root, 'runtime/companion-preview');
const threadArg = process.argv.indexOf('--codex-thread');
const codexThread = threadArg >= 0 ? process.argv[threadArg + 1] : null;
if (threadArg >= 0 && !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(codexThread || '')) throw new Error('Invalid Codex task ID');
fs.mkdirSync(output, { recursive: true });
app.setPath('userData', path.join(output, 'user-data'));
let server, win, hitTimer, layer;
const log = text => fs.appendFileSync(path.join(output, 'preview.log'), text + '\n');
app.whenReady().then(async () => {
  const target = screen.getAllDisplays().find(item => item.id !== screen.getPrimaryDisplay().id);
  if (!target) { log('No secondary display'); app.quit(); return; }
  const { resolveCompanionDesktop } = await import(pathToFileURL(path.join(root, 'electron/dist/main/windowPlacement.js')).href);
  const desktop = () => resolveCompanionDesktop(screen.getAllDisplays(), screen.getPrimaryDisplay());
  const probe = net.createServer();
  const portIndex = process.argv.indexOf('--port');
  const requestedPort = portIndex >= 0 ? Number(process.argv[portIndex + 1]) : 0;
  if (!Number.isInteger(requestedPort) || requestedPort < 0 || requestedPort > 65535) throw new Error('Invalid preview port');
  await new Promise((resolve, reject) => { probe.once('error', reject); probe.listen(requestedPort, '127.0.0.1', resolve); });
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const { resolvePythonCommand } = await import(pathToFileURL(path.join(root, 'electron/dist/main/pythonRuntime.js')).href);
  const interpreter = resolvePythonCommand({ projectRoot: root });
  server = spawn(interpreter, [path.join(root, 'tools/companion_preview_server.py'), '--port', String(port), ...(codexThread ? ['--codex-thread', codexThread] : [])], { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stderr.on('data', data => log(String(data)));
  server.stdout.on('data', data => log(String(data)));
  let ready = false;
  for (let i = 0; i < 60; i++) {
    try { ready = (await fetch(origin + '/ui/index.html')).ok; } catch {}
    if (ready) break;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  if (!ready) { log('Preview server did not start'); app.quit(); return; }
  win = new BrowserWindow({ ...desktop().bounds, frame: false, transparent: true, backgroundColor: '#00000000',
    focusable: process.platform !== 'win32', show: false,
    hasShadow: false, alwaysOnTop: !process.argv.includes('--follow-active-window'), fullscreenable: false, skipTaskbar: false,
    title: codexThread ? 'Amadeus · Codex 真实任务联调' : 'Amadeus 悬浮预览 · 示例事件', webPreferences: { preload: path.join(__dirname, 'companion-preview-preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true } });
  win.setMenu(null);
  win.setAlwaysOnTop(!process.argv.includes('--follow-active-window'), 'screen-saver');
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  let regions = [];
  ipcMain.handle('preview.connection', () => ({ url: `ws://127.0.0.1:${port}/ws`, protocols: [] }));
  ipcMain.handle('preview.close', () => app.quit());
  ipcMain.handle('preview.display', () => desktop());
  ipcMain.handle('preview.explain', () => dialog.showMessageBox(win, { message: '这是示例任务。预览没有连接 Codex，不会提交任何回答。', buttons: ['知道了'] }));
  ipcMain.handle('preview.codex', async (_event, threadId) => {
    if (!codexThread) { log(`Fixture open source: ${threadId}`); return false; }
    if (!codexThread || threadId !== codexThread) return false;
    await shell.openExternal(`codex://threads/${codexThread}`);
    return true;
  });
  ipcMain.handle('preview.hit', (_event, value) => { regions = value; fs.writeFileSync(path.join(output, 'hit-regions.json'), JSON.stringify(value)); return true; });
  let ignoring = false;
  let passthrough = !process.argv.includes('--interactive-check');
  hitTimer = setInterval(() => {
    if (!win || win.isDestroyed()) return;
    const p = screen.getCursorScreenPoint(), bounds = win.getBounds();
    const inside = !passthrough || regions.some(r => p.x >= bounds.x + r.x && p.x <= bounds.x + r.x + r.width && p.y >= bounds.y + r.y && p.y <= bounds.y + r.y + r.height);
    if (ignoring !== !inside) { ignoring = !inside; win.setIgnoreMouseEvents(ignoring, { forward: true }); }
  }, 40);
  win.webContents.on('before-input-event', async (_event, input) => {
    if (input.type !== 'keyDown') return;
    if (input.key === 'Escape' && input.control) return app.quit();
    if (input.key.toLowerCase() === 'r' && input.control) { win.webContents.reload(); return; }
    // Automation hit-tests coordinates before moving the pointer; pause only
    // preview passthrough with P for that check, then restore it with P.
    if (input.key.toLowerCase() === 'p') passthrough = !passthrough;
    if (['0', '1', '2', '3'].includes(input.key)) await fetch(origin + '/scene/' + input.key, { method: 'POST' });
    if (input.key.toLowerCase() === 's') {
      const image = await win.webContents.capturePage();
      const stamp = Date.now();
      fs.writeFileSync(path.join(output, `preview-${stamp}.png`), image.toPNG());
      for (const display of desktop().displays) {
        const region = { x: display.workArea.x - desktop().bounds.x, y: display.workArea.y - desktop().bounds.y,
          width: display.workArea.width, height: display.workArea.height };
        const captured = await win.webContents.capturePage(region);
        fs.writeFileSync(path.join(output, `preview-${stamp}-display-${display.id}.png`), captured.toPNG());
      }
      const geometry = await win.webContents.executeJavaScript(`({
        preferences:JSON.parse(localStorage.getItem('amadeus.companion.layout.v1') || 'null'),
        editing:document.querySelector('.floating-companion').classList.contains('is-layout-editing'),
        cards:[...document.querySelectorAll('[data-task-id]')].filter(e=>getComputedStyle(e).visibility==='visible').map(e=>({id:e.dataset.taskId,...e.getBoundingClientRect().toJSON()})),
        projects:[...document.querySelectorAll('[data-project-id]')].map(e=>({id:e.dataset.projectId,...e.getBoundingClientRect().toJSON()})),
        character:document.querySelector('.floating-companion-render').getBoundingClientRect().toJSON(),
        edges:[...document.querySelectorAll('[data-edge-task]')].map(e=>({id:e.dataset.edgeTask,path:e.querySelector('path').getAttribute('d')}))
      })`);
      fs.writeFileSync(path.join(output, `preview-${stamp}.json`), JSON.stringify({ ...geometry, nativeBounds:win.getBounds(), contentBounds:win.getContentBounds(), passthrough, regions }, null, 2));
    }
  });
  win.webContents.on('console-message', (_event, level, message) => { if (level >= 2) log(message); });
  await win.loadURL(origin + '/ui/index.html?companionWindow=1');
  win.showInactive();
  win.setBounds(desktop().bounds, false);
  if (process.argv.includes('--follow-active-window')) {
    const handle = win.getNativeWindowHandle();
    const hwnd = String(handle.length >= 8 ? handle.readBigUInt64LE() : handle.readUInt32LE());
    layer = spawn(interpreter, [path.join(__dirname, 'companion_window_layer.py'), '--window', hwnd],
      { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    layer.stdout.on('data', data => log('[layer] ' + String(data).trim()));
    layer.stderr.on('data', data => log('[layer-error] ' + String(data).trim()));
    win.on('closed', () => layer?.kill());
  }
  log(JSON.stringify({requested:desktop().bounds, native:win.getBounds(), content:win.getContentBounds()}));
  fs.writeFileSync(path.join(output, 'running.json'), JSON.stringify({ pid: process.pid, origin, display: target.workArea, desktop:desktop(), codexThread }, null, 2));
  if (process.argv.includes('--focus-probe')) {
    await fetch(origin+'/families/2',{method:'POST'});
    await new Promise(resolve=>setTimeout(resolve,1400));
    // Preview-only placement on the primary screen permits native mouse checks
    // even when the automation driver's capture is restricted to that screen.
    const saved=await win.webContents.executeJavaScript(`(() => {
      const key='amadeus.companion.layout.v1',saved=JSON.parse(localStorage.getItem(key));
      const display=Object.keys(saved.displays)[0],profile=saved.displays[display][saved.mode];
      profile.placements={projects:{},tasks:{'organic-0-0':{x:-1740,y:160,scale:1}}};
      return JSON.stringify(saved);
    })()`);
    // Unmount before setting the final fixture preference so pagehide cannot
    // overwrite a new placement with the still-mounted scene's old state.
    await win.loadURL(origin+'/openapi.json');
    await win.webContents.executeJavaScript(`localStorage.setItem('amadeus.companion.layout.v1',${JSON.stringify(saved)})`);
    await win.loadURL(origin+'/ui/index.html?companionWindow=1');
    await new Promise(resolve=>setTimeout(resolve,1800));
    await require('./companion-focus-probe.cjs')(win,output);
  }
  if (process.argv.includes('--verify-project-map') && !codexThread) {
    await require('./verify-companion-project-map.cjs')({win,desktop:desktop(),origin,output});
    log('Verified project overview map in production Electron renderer');
    if(process.argv.includes('--exit-after-verify')) app.quit();
  }
  if (process.argv.includes('--verify-cross-display') && !codexThread) {
    await require('./verify-companion-cross-display.cjs')({win,desktop:desktop(),origin,output});
    log('Verified cross-display Chromium pointer integration');
    if(process.argv.includes('--exit-after-verify')) app.quit();
  }
  if (process.argv.includes('--verify-organic') && !codexThread) {
    const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
    const evidence = [];
    await pause(4000);
    for (const count of [1,2,3,4,5,4,3,2,1]) {
      await fetch(origin + '/organic/' + count, {method:'POST'});
      await pause(1300);
      const bounds = await win.webContents.executeJavaScript(`({
        viewport:{width:innerWidth,height:innerHeight},
        scroll:[document.querySelector('.companion-constellation').clientHeight,document.querySelector('.companion-constellation').scrollHeight],
        cards:[...document.querySelectorAll('.companion-task-position:not(.is-hidden) .companion-thought-card:not(.is-stack-back)')].map(e=>({id:e.dataset.taskId,...e.getBoundingClientRect().toJSON()})),
        badges:[...document.querySelectorAll('.companion-project-badge')].map(e=>({id:e.dataset.projectId,...e.getBoundingClientRect().toJSON()}))
      })`);
      if(bounds.badges.length!==count || bounds.scroll[1]>bounds.scroll[0]+1) throw new Error('Organic project visibility or scrolling failed');
      const rects=[...bounds.cards,...bounds.badges];
      for(let i=0;i<rects.length;i++) {
        const r=rects[i];
        if(r.left<0 || r.top<0 || r.right>bounds.viewport.width || r.bottom>bounds.viewport.height) throw new Error('Organic node outside display');
        for(let j=i+1;j<rects.length;j++) {
          const b=rects[j];
          if(r.left<b.right && b.left<r.right && r.top<b.bottom && b.top<r.bottom) throw new Error('Organic nodes overlap');
        }
      }
      evidence.push({count,...bounds});
      fs.writeFileSync(path.join(output,`organic-${evidence.length}-${count}-projects.png`),(await win.webContents.capturePage()).toPNG());
    }
    fs.writeFileSync(path.join(output,'organic-verification.json'),JSON.stringify({verified:true,at:new Date().toISOString(),workArea:target.workArea,evidence},null,2));
    log('Verified organic one-to-five project lifecycle');
  }
  if (process.argv.includes('--verify-layout') && !codexThread) {
    // Fixture source only. These assertions exercise the production renderer;
    // native pointer checks are performed separately through the desktop driver.
    const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
    const js = source => win.webContents.executeJavaScript(source);
    const scene = async name => { await fetch(origin + name, { method:'POST' }); await pause(1000); };
    const check = async (source, message) => { if (!await js(source)) throw new Error(message); };
    const capture = async name => fs.writeFileSync(path.join(output,name+'.png'),(await win.webContents.capturePage()).toPNG());
    const click = async selector => { await js(`document.querySelector(${JSON.stringify(selector)}).click()`); await pause(950); };
    const countCards = () => js("document.querySelectorAll('.companion-thought-card').length");
    await pause(4500);
    await scene('/scene/15');
    await check("document.querySelectorAll('.companion-project-badge').length===5", 'Five project headings must stay visible');
    const bounds = await js(`({
      viewport:{width:innerWidth,height:innerHeight},
      scroll:[document.querySelector('.companion-constellation').clientHeight,document.querySelector('.companion-constellation').scrollHeight],
      cards:[...document.querySelectorAll('.companion-thought-card:not(.is-stack-back)')].filter(e=>getComputedStyle(e).visibility==='visible').map(e=>({id:e.dataset.taskId,...e.getBoundingClientRect().toJSON()})),
      badges:[...document.querySelectorAll('.companion-project-badge')].map(e=>e.getBoundingClientRect().toJSON())
    })`);
    if (bounds.cards.length<5 || bounds.scroll[1]>bounds.scroll[0]+1) throw new Error('Five projects require overview scrolling');
    for(const rect of [...bounds.cards,...bounds.badges]) if(rect.x<0||rect.y<0||rect.right>bounds.viewport.width||rect.bottom>bounds.viewport.height) throw new Error('Project or task outside secondary work area');
    await capture('constellation-five-projects');
    await js("window.__taskNodes=[...document.querySelectorAll('[data-task-id]')]; window.__startRects=window.__taskNodes.map(e=>e.getBoundingClientRect().toJSON())");
    await js("document.querySelector('[data-task-id=\"preview-question\"] .companion-thought-open').click()");
    await pause(180);
    await check("window.__taskNodes.filter(e=>e.parentElement.getAnimations().some(a=>a.playState==='running')).length>3", 'Individual cards did not animate');
    await capture('constellation-five-motion');
    await pause(750);
    await check("window.__taskNodes.every(e=>document.querySelector('[data-task-id=\"'+e.dataset.taskId+'\"]')===e)", 'Task nodes were replaced during expansion');
    await check("!document.querySelector('.companion-focus-panel')", 'Stack should open project before task');
    await check("document.querySelectorAll('.companion-task-position:not(.is-hidden) .companion-thought-card:not(.is-miniature)').length===4", 'Project task page should display four independent cards');
    await capture('constellation-project-expanded');
    await click('.companion-project-task-pages');
    await check("document.querySelectorAll('.companion-task-position:not(.is-hidden) .companion-thought-card:not(.is-miniature)').length===1", 'Remaining task inaccessible on next page');
    await click('.companion-project-task-pages');
    await click('[data-task-id="preview-question"] .companion-thought-open');
    await check("document.querySelectorAll('[data-task-id=\"preview-question\"]').length===1 && document.querySelectorAll('[data-edge-task=\"preview-question\"]').length===1", 'Focused task must own only one card and connection');
    await check("document.querySelectorAll('.companion-child-thoughts>button').length===1", 'Subagent branch missing');
    await check("document.querySelectorAll('.companion-reading-card').length===3", 'Progress should be individual cards');
    await capture('constellation-task-reading');
    await click('.companion-child-thoughts>button');
    await check("document.querySelector('.companion-focus-anchor').textContent.includes('子代理任务')", 'Agent lacks distinct identity');
    await click('.companion-parent-task');
    await click('.companion-focus-dismiss');
    await check("!document.querySelector('.companion-focus-panel') && document.querySelector('.companion-return-overview')", 'Task back must restore project layer');
    await click('.companion-focus-dismiss');
    await check("!document.querySelector('.companion-return-overview')", 'Second back must restore overview');
    // A project with exactly two tasks must still have exactly two edges when focused.
    await scene('/scene/4');
    await click('[data-task-id="preview-question"] .companion-thought-open');
    await check("document.querySelectorAll('[data-edge-project=\"project:preview-amadeus\"]').length===2", 'Two-task project gained a third apparent task');
    await click('.companion-focus-dismiss');
    await click('[data-task-id="preview-question"] .companion-acknowledge');
    await check("!document.querySelector('[data-task-id=\"preview-question\"] .companion-acknowledge') && document.querySelector('[data-task-id=\"preview-question\"]')", 'Acknowledgement should remove only the button');
    await click('[data-task-id="preview-question"] .companion-open-source');
    await check("!document.querySelector('.companion-focus-panel')", 'Quick source action unexpectedly expanded task');
    await click('[data-task-id="preview-end"] .companion-thought-open');
    if(!await js("document.querySelector('.companion-focus-panel')!==null")) await click('[data-task-id="preview-end"] .companion-thought-open');
    await check("document.querySelector('[data-reading-id=\"current\"] table') && document.querySelector('[data-reading-id=\"current\"] input[type=\"checkbox\"]') && document.querySelector('[data-reading-id=\"current\"] pre code')", 'Markdown rendering failed');
    await click('[data-reading-id="current"] .companion-reading-toggle');
    await capture('constellation-markdown');
    await click('.companion-focus-dismiss');
    await scene('/retention/start');
    if (await countCards()!==3) throw new Error('Recent fixtures missing before eight-hour boundary');
    await pause(5000);
    if (await countCards()!==0) throw new Error('Inactive fixtures did not expire');
    await scene('/retention/replay');
    if (await countCards()!==0) throw new Error('Old snapshot resurrected cards');
    await scene('/retention/activity');
    if (await countCards()!==1) throw new Error('New activity did not restore card');
    await click('.companion-thought-dismiss');
    await scene('/retention/replay');
    if (await countCards()!==0) throw new Error('Manual dismissal did not survive polling');
    await scene('/retention/clear');
    await scene('/scene/15');
    fs.writeFileSync(path.join(output,'constellation-five-verification.json'),JSON.stringify({verified:true,at:new Date().toISOString(),workArea:target.workArea,bounds,
      checks:['five visible projects without scrolling','same-project priority stack','independent card motion and stable identity','project pages','one card and edge per task','two-step blank return','nested agent','individual progress cards','Markdown','quick acknowledgement and source','8h inactivity','manual dismissal']},null,2));
    log('Verified five-project layout and layered navigation');
    if (process.argv.includes('--exit-after-verify')) app.quit();
  }
  if (process.argv.includes('--verify-reading') && !codexThread) {
    const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
    const js = source => win.webContents.executeJavaScript(source);
    const evidence = [];
    await pause(4000);
    for (const kind of ['short','prose','markdown','long-title']) {
      await fetch(origin + '/reading/' + kind, { method:'POST' });
      await pause(900);
      await js("document.querySelector('[data-task-id=\"preview-end\"] .companion-thought-open').click()");
      await pause(1400);
      if(!await js("document.querySelector('.companion-focus-panel')!==null")) {
        await js("document.querySelector('[data-task-id=\"preview-end\"] .companion-thought-open').click()");
        await pause(950);
      }
      const measure = await js(`(() => {
        const anchor=document.querySelector('.companion-focus-anchor'), body=document.querySelector('[data-reading-id="current"]');
        const content=body.querySelector('.companion-markdown'), title=anchor.querySelector('h2');
        return { heading:anchor.getBoundingClientRect().toJSON(), result:body.getBoundingClientRect().toJSON(),
          title:title.textContent, headingContentHeight:anchor.querySelector('.companion-focus-heading').offsetHeight,
          actions:[...anchor.querySelectorAll('.companion-card-actions button')].map(b=>b.getBoundingClientRect().toJSON()),
          previewHeight:content.clientHeight, fullHeight:content.scrollHeight, lineHeight:parseFloat(getComputedStyle(content).lineHeight),
          fade:getComputedStyle(content).maskImage, toggle:!!body.querySelector('.companion-reading-toggle'),
          emptyBranch:!!document.querySelector('.companion-child-thoughts'),
          tableRows:body.querySelectorAll('tbody tr').length
        }
      })()`);
      if(measure.headingContentHeight>measure.heading.height) throw new Error('Heading content was clipped');
      if(measure.result.y<measure.heading.bottom || measure.result.y-measure.heading.bottom>30) throw new Error('Reading area does not follow natural title height');
      for(const action of measure.actions) if(action.right>measure.heading.right || action.bottom>measure.heading.bottom) throw new Error('Heading actions escaped the title card');
      if(measure.emptyBranch) throw new Error('Empty subagent placeholder wastes reading space');
      if(kind!=='long-title' && measure.heading.height>120) throw new Error('Short heading reserves too much empty space');
      if(kind==='short' && (measure.toggle || measure.previewHeight<measure.fullHeight)) throw new Error('Short result should display in full');
      if(kind==='prose' && (measure.previewHeight<measure.lineHeight*6.5 || !measure.toggle || measure.fade!=='none')) throw new Error('Long result preview lost readable lines');
      if(kind==='markdown' && measure.tableRows<2) throw new Error('Markdown table failed');
      fs.writeFileSync(path.join(output,'reading-'+kind+'.png'),(await win.webContents.capturePage()).toPNG());
      if(kind==='prose') {
        await js("document.querySelector('[data-reading-id=\"current\"] .companion-reading-toggle').click()");
        await pause(250);
        if(!await js("document.querySelector('[data-reading-id=\"current\"] .companion-markdown').clientHeight >= document.querySelector('[data-reading-id=\"current\"] .companion-markdown').scrollHeight")) throw new Error('Expanded result still clipped');
      }
      evidence.push({kind,...measure});
      await js("document.querySelector('.companion-focus-dismiss').click()");
      await pause(900);
    }
    await fetch(origin + '/reading/prose', {method:'POST'});
    await pause(900);
    await js("document.querySelector('[data-task-id=\"preview-end\"] .companion-thought-open').click()");
    await pause(1400);
    fs.writeFileSync(path.join(output,'reading-verification.json'),JSON.stringify({verified:true,at:new Date().toISOString(),workArea:target.workArea,evidence},null,2));
    log('Verified natural heading height and expanded default reading previews');
    if(process.argv.includes('--exit-after-verify')) app.quit();
  }
  screen.on('display-removed', (_event, display) => { if (display.id === target.id) app.quit(); });
}).catch(error => { log(String(error.stack || error)); app.exit(1); });
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { clearInterval(hitTimer); server?.kill(); });
