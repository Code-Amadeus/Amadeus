import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { parseOptions, childArgs, classifyExit, controlPage } from '../tools/probes/frame-video/shutdown_control.mjs';

const basic = ['--electron', 'C:/Electron/electron.exe', '--output', 'C:/temporary/control'];

test('controls reject contradictory coverage and require fixture only for decoding modes', () => {
  assert.equal(parseOptions(basic).mode, 'blank');
  assert.equal(parseOptions([...basic, '--mode', 'pixi', '--context-loss']).contextLoss, true);
  for (const mode of ['decode', 'video']) {
    assert.throws(() => parseOptions([...basic, '--mode', mode]), /fixture/);
    assert.equal(parseOptions([...basic, '--mode', mode, '--fixture', 'C:/reference']).mode, mode);
  }
  assert.throws(() => parseOptions([...basic, '--context-loss']), /WebGL/);
  assert.throws(() => parseOptions([...basic, '--mode', 'decode', '--fixture', 'C:/reference', '--context-loss']), /WebGL/);
  for (const option of [['--mode', 'unknown'], ['--seconds', 'NaN'], ['--timeout-ms', '0'], ['--decode-frames', '1000'],
    ['--exit-method', 'terminate'], ['--cleanup', 'none']]) assert.throws(() => parseOptions([...basic, ...option]), /Invalid/);
});

test('the launcher passes paths and control flags as separate arguments without shell quoting', () => {
  const options = parseOptions([...basic, '--mode', 'video', '--fixture', 'C:/Reference with spaces/clip',
    '--context-loss', '--finish', '--trace', '--cleanup', 'window', '--exit-method', 'quit', '--high-performance-gpu']);
  const args = childArgs(options);
  assert.ok(args.includes('--child')); assert.ok(args.includes('--context-loss')); assert.ok(args.includes('--trace'));
  assert.match(args[args.indexOf('--fixture') + 1], /Reference with spaces/);
  assert.equal(args[args.indexOf('--cleanup') + 1], 'window');
  assert.equal(args[args.indexOf('--exit-method') + 1], 'quit');
});

test('a native GPU error after app.exit makes an exit-zero child an observed-error control', () => {
  const event = { name: 'app-exit-requested', atEpochMs: 100 };
  const line = { text: '[7624:1013/072123.115:ERROR:gpu/ipc/client/command_buffer_proxy_impl.cc:490] GPU state invalid after WaitForGetOffsetInRange.',
    receivedEpochMs: 200, lastStageAtReceipt: 'app-exit-requested' };
  const result = classifyExit({ exitCode: 0, signal: null, timedOut: false, events: [event], stderrLines: [line], childResult: { completed: true } });
  assert.equal(result.outcome, 'gpu-state-invalid-observed'); assert.equal(result.gpuStateInvalidCount, 1);
  assert.equal(result.nativeErrorCount, 1); assert.equal(result.stabilityValidated, false);
  assert.equal(result.gpuErrors[0].lastStageAtReceipt, 'app-exit-requested');
});

test('missing or timed-out controls never become a clean control merely because the child exits zero', () => {
  const base = { exitCode: 0, signal: null, timedOut: false, events: [], stderrLines: [], childResult: { completed: true } };
  assert.equal(classifyExit(base).outcome, 'no-gpu-state-invalid-observed');
  assert.equal(classifyExit(base).stabilityValidated, false);
  assert.equal(classifyExit({ ...base, childResult: undefined }).outcome, 'control-incomplete');
  assert.equal(classifyExit({ ...base, timedOut: true }).outcome, 'control-incomplete');
  assert.equal(classifyExit({ ...base, exitCode: 1 }).outcome, 'control-incomplete');
});

test('blank control completes without requesting WebGL, Pixi, WebCodecs or fixture bytes', async () => {
  const options = parseOptions([...basic, '--seconds', '0']);
  const html = controlPage(options);
  assert.doesNotMatch(html, /<script src="\/vendor.js">/);
  const events = [];
  const unexpected = () => { throw Error('Blank control exercised a GPU API'); };
  const context = { window: {}, document: { createElement: unexpected }, fetch: unexpected,
    FrameDecoder: class { constructor() { unexpected(); } }, createVideoOutput: unexpected,
    VideoDecoder: { isConfigSupported: unexpected }, setTimeout, clearTimeout, performance,
    console: { info(message) { events.push(JSON.parse(message.slice('CONTROL_PAGE_EVENT '.length))); } } };
  const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1].replace(/^\s*import .*$/gm, '');
  vm.runInNewContext(script, context);
  const result = await context.window.controlRun();
  assert.equal(result.completed, true); assert.equal(result.webglCreated, false); assert.equal(result.webCodecsCreated, false);
  assert.deepEqual(events.map(event => event.name), ['page-exercise-start', 'page-exercise-end', 'page-cleanup-start', 'page-cleanup-end']);
});

test('all generated control page scripts are syntactically valid', () => {
  for (const mode of ['blank', 'webgl', 'pixi', 'decode', 'video']) {
    const options = parseOptions([...basic, '--mode', mode, '--fixture', 'C:/reference']);
    const script = controlPage(options).match(/<script type="module">([\s\S]*?)<\/script>/)[1].replace(/^\s*import .*$/gm, '');
    assert.doesNotThrow(() => new vm.Script(script));
  }
});
