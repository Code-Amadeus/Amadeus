// Isolated loopback page. Does not launch the product, models, or wallpaper host.
import { app, BrowserWindow, contentTracing } from 'electron';
import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const deadline = async (promise, milliseconds, name) => {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(Error(name + ' timed out after ' + milliseconds + 'ms')), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
};

// Do not await app.whenReady at module scope: Electron waits for ESM evaluation
// before emitting ready. Catch startup errors too, avoiding a native error dialog.
async function main() {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
  const { values } = parseArgs({ args: process.argv.slice(2), options: {
    fixture: { type: 'string' }, output: { type: 'string' }, seconds: { type: 'string', default: '30' },
    'cold-runs': { type: 'string', default: '20' }, hardware: { type: 'string', default: 'prefer-hardware' },
    'queue-capacity': { type: 'string', default: '16' }, 'identity-rounds': { type: 'string', default: '2' },
    targets: { type: 'string' }, 'stall-ms': { type: 'string', default: '0' },
    'stall-every-ms': { type: 'string', default: '1000' }, 'reset-every-ms': { type: 'string', default: '0' },
    'skip-context-loss': { type: 'boolean', default: false }, 'shutdown-mode': { type: 'string', default: 'graceful' },
    'shutdown-settle-ms': { type: 'string', default: '250' }, 'timeout-ms': { type: 'string' },
    'skip-gpu-info': { type: 'boolean', default: false },
    'high-performance-gpu': { type: 'boolean', default: false }, trace: { type: 'boolean', default: false },
  } });
  if (!values.fixture || !values.output) throw Error('Required: --fixture DIRECTORY --output NEW_DIRECTORY');
  const options = { seconds: Number(values.seconds), coldRuns: Number(values['cold-runs']), hardware: values.hardware,
    capacity: Number(values['queue-capacity']), identityRounds: Number(values['identity-rounds']),
    targets: values.targets === undefined ? null : values.targets.split(',').map(Number),
    stallMs: Number(values['stall-ms']), stallEveryMs: Number(values['stall-every-ms']),
    resetEveryMs: Number(values['reset-every-ms']), contextLoss: !values['skip-context-loss'],
    shutdownMode: values['shutdown-mode'], shutdownSettleMs: Number(values['shutdown-settle-ms']) };
  const timeoutMs = values['timeout-ms'] === undefined
    ? Math.ceil(options.seconds * 1000 + Math.max(120000, options.coldRuns * 5000)) : Number(values['timeout-ms']);
  if (!(options.seconds >= 1 && options.seconds <= 3600)
      || !Number.isInteger(options.coldRuns) || options.coldRuns < 1 || options.coldRuns > 1000
      || !Number.isInteger(options.capacity) || options.capacity < 2 || options.capacity > 256
      || !Number.isInteger(options.identityRounds) || options.identityRounds < 1 || options.identityRounds > 100
      || !['prefer-hardware', 'prefer-software', 'no-preference'].includes(options.hardware)
      || options.targets?.some(target => !Number.isInteger(target) || target < 0)
      || !Number.isFinite(options.stallMs) || options.stallMs < 0 || options.stallMs > 60000
      || !Number.isFinite(options.stallEveryMs) || options.stallEveryMs < Math.max(1, options.stallMs)
      || !Number.isFinite(options.resetEveryMs) || options.resetEveryMs < 0
      || !['graceful', 'immediate'].includes(options.shutdownMode)
      || !Number.isInteger(options.shutdownSettleMs) || options.shutdownSettleMs < 0 || options.shutdownSettleMs > 10000
      || !Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 7200000) throw Error('Invalid probe options');
  const fixture = path.resolve(values.fixture), output = path.resolve(values.output);
  await fs.mkdir(output, { recursive: false });
  app.setPath('userData', path.join(output, 'profile'));
  app.commandLine.appendSwitch('disable-background-timer-throttling');
  app.commandLine.appendSwitch('enable-logging', 'file');
  app.commandLine.appendSwitch('log-file', path.join(output, 'chromium.log'));
  if (values['high-performance-gpu']) app.commandLine.appendSwitch('force-high-performance-gpu');
  app.on('window-all-closed', () => {});
  const names = ['probe.html', 'ivf.mjs', 'decoder.mjs', 'pixi-output.mjs'];
  const routes = new Map(names.map(name => ['/tools/probes/frame-video/' + name, path.join(root, 'tools/probes/frame-video', name)]));
  routes.set('/render/web/vendor/pixi.min.js', path.join(root, 'render/web/vendor/pixi.min.js'));
  for (const name of ['clip.json', 'clip.ivf', 'color.ivf', 'alpha.ivf']) routes.set('/fixture/' + name, path.join(fixture, name));
  const server = http.createServer(async (req, res) => {
    const file = routes.get(new URL(req.url, 'http://localhost').pathname);
    if (!file || req.method !== 'GET') { res.writeHead(404).end(); return; }
    try {
      const data = await fs.readFile(file);
      const type = { '.js': 'text/javascript', '.mjs': 'text/javascript', '.html': 'text/html', '.json': 'application/json' }[path.extname(file)] || 'application/octet-stream';
      res.writeHead(200, { 'Content-Type': type, 'Content-Length': data.length, 'Cache-Control': 'no-store' }); res.end(data);
    } catch (error) { res.writeHead(500).end(); console.error('Fixture route failed: ' + String(error)); }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  let window, sampler, tracing = false, result, activePhase = 'host-startup';
  const samples = [], messages = [], hostEvents = [], logSnapshots = [], sourceSha256 = {};
  for (const name of names) sourceSha256[name] = createHash('sha256').update(await fs.readFile(routes.get('/tools/probes/frame-video/' + name))).digest('hex');
  const write = (name, value) => fs.writeFile(path.join(output, name), JSON.stringify(value, null, 2));
  const hostEvent = name => { activePhase = name; hostEvents.push({ name, atEpochMs: Date.now() }); };
  const sample = () => samples.push({ atEpochMs: Date.now(), phase: activePhase, processes: app.getAppMetrics().map(p => ({
    pid: p.pid, type: p.type, cpuSeconds: p.cpu.cumulativeCPUUsage, privateKiB: p.memory.privateBytes,
  })) });
  const snapshotLog = async stage => {
    try {
      const contents = await fs.readFile(path.join(output, 'chromium.log'), 'utf8');
      const lines = contents.split(/\r?\n/);
      const gpuStateInvalid = lines.filter(line => line.includes('GPU state invalid after WaitForGetOffsetInRange'));
      const errors = lines.filter(line => /\bERROR\b/.test(line));
      logSnapshots.push({ stage, atEpochMs: Date.now(), bytes: Buffer.byteLength(contents), gpuStateInvalid, errors });
    } catch (error) {
      logSnapshots.push({ stage, atEpochMs: Date.now(), unavailable: String(error) });
    }
  };
  try {
    await deadline(app.whenReady(), 30000, 'Electron startup');
    window = new BrowserWindow({ show: false, width: 1100, height: 1100,
      webPreferences: { nodeIntegration: false, contextIsolation: true, backgroundThrottling: false } });
    let rejectRendererFailure;
    const rendererFailure = new Promise((_resolve, reject) => { rejectRendererFailure = reject; });
    // Attach an observer before loadURL so a crash during navigation does not
    // become an unhandled rejection before the execution race is installed.
    rendererFailure.catch(() => {});
    window.webContents.on('console-message', event => {
      const message = String(event.message);
      messages.push({ atEpochMs: Date.now(), phase: activePhase, message });
      if (message.startsWith('FRAME_VIDEO_EVENT ')) {
        try { activePhase = JSON.parse(message.slice('FRAME_VIDEO_EVENT '.length)).phase; } catch {}
      }
    });
    window.webContents.on('render-process-gone', (_event, details) => {
      void write('renderer-failure.json', details);
      rejectRendererFailure(Error('Renderer exited: ' + details.reason));
    });
    window.on('unresponsive', () => rejectRendererFailure(Error('Probe renderer became unresponsive')));
    sampler = setInterval(sample, 1000); sample();
    if (values.trace) {
      await deadline(contentTracing.startRecording({ included_categories: ['gpu', 'media', 'blink.user_timing', 'disabled-by-default-media', 'disabled-by-default-gpu.service'] }), 10000, 'Trace start');
      tracing = true;
    }
    await deadline(window.loadURL('http://127.0.0.1:' + server.address().port + '/tools/probes/frame-video/probe.html'), 30000, 'Probe page load');
    result = await deadline(Promise.race([window.webContents.executeJavaScript('window.runProbe(' + JSON.stringify(options) + ')'), rendererFailure]),
      timeoutMs, 'Probe execution');
    hostEvent('page-result-received'); await snapshotLog(activePhase); sample();
    for (const capture of result.captures) {
      await fs.writeFile(path.join(output, 'hardware-' + String(capture.sourceIndex).padStart(6, '0') + '.png'), Buffer.from(capture.png.split(',')[1], 'base64'));
    }
    delete result.captures;
    result.host = { versions: process.versions, platform: process.platform, arch: process.arch,
      sourceCommit: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8', windowsHide: true }).trim(),
      sourceSha256, hiddenWindow: true, options: { ...options, timeoutMs, highPerformanceGpu: values['high-performance-gpu'],
        skipCompleteGpuInfo: values['skip-gpu-info'] },
      gpuFeatureStatus: app.getGPUFeatureStatus() };
    // Complete GPU queries reproduced a shutdown diagnostic in a blank window.
    // Keep the query optional for isolation; WebGL identity and codec trace stay.
    if (!values['skip-gpu-info']) await write('gpu-info.json', await deadline(app.getGPUInfo('complete'), 10000, 'GPU info'));
  } catch (error) {
    await write('failure.json', { error: String(error), stack: error.stack, phase: activePhase });
    if (window && !window.isDestroyed() && !window.webContents.isDestroyed()) {
      try {
        const partial = await deadline(window.webContents.executeJavaScript('JSON.stringify(window.frameVideoPartial ?? null)'), 2000, 'Partial evidence');
        await fs.writeFile(path.join(output, 'partial-result.json'), partial);
      } catch (partialError) { await write('partial-evidence-failure.json', { error: String(partialError) }); }
    }
    console.error(error); process.exitCode = 1;
  } finally {
    clearInterval(sampler);
    hostEvent('host-window-destroy-start'); await snapshotLog(activePhase);
    window?.destroy(); hostEvent('host-window-destroy-end');
    if (options.shutdownMode === 'graceful') await delay(options.shutdownSettleMs);
    await snapshotLog('after-window-settle');
    if (tracing) {
      try { await deadline(contentTracing.stopRecording(path.join(output, 'trace.json')), 10000, 'Trace stop'); }
      catch (error) { await write('trace-failure.json', { error: String(error) }); process.exitCode = 1; }
    }
    await snapshotLog('after-trace-stop');
    const lastLog = [...logSnapshots].reverse().find(value => !value.unavailable);
    const shutdown = { hostEvents, logSnapshots, chromiumLog: 'chromium.log',
      gpuStateInvalidCount: lastLog?.gpuStateInvalid.length ?? null,
      attribution: 'Snapshots show when a message was available, not when the GPU emitted it. Preserve Chromium timestamps and trace for attribution.',
      applicationExitCoverage: 'The in-process harness cannot inspect messages emitted after app.exit; retain launcher stderr for that final boundary.' };
    if (result) {
      result.host.shutdown = shutdown;
      if (shutdown.gpuStateInvalidCount) result.validation.passed = false;
      if (!result.validation.passed) process.exitCode = 1;
      await write('result.json', result); console.log(JSON.stringify(result));
    }
    await write('shutdown.json', shutdown);
    await write('process-samples.json', samples); await write('console.json', messages);
    server.closeAllConnections();
    try { await deadline(new Promise(resolve => server.close(resolve)), 3000, 'Probe HTTP shutdown'); }
    catch (error) { console.error(error); process.exitCode = 1; }
    app.exit(process.exitCode || 0);
  }
}
main().catch(error => { console.error(error); app.exit(1); });
