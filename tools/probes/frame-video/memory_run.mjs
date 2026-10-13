// External lifetime owner: Windows counters survive Electron's final exit.
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { createHash } from 'node:crypto';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function main() {
  const args = process.argv.slice(2), separator = args.indexOf('--');
  if (separator < 0) throw Error('Separate the product probe arguments with --');
  const { values } = parseArgs({ args: args.slice(0, separator), options: {
    electron: { type: 'string' }, probe: { type: 'string' }, output: { type: 'string' },
    'no-sampler': { type: 'boolean', default: false },
  } });
  if (!values.electron || !values.probe || !values.output) throw Error('Required: --electron --probe --output');
  const pass = args.slice(separator + 1);
  if (pass.includes('--output') || pass.includes('--gpu-memory')) throw Error('Launcher owns output and GPU memory flags');
  const output = path.resolve(values.output), counterDirectory = output + '-memory';
  const sampled = !values['no-sampler'];
  const sourceSha256 = {};
  for (const name of ['memory_run.mjs', 'gpu_memory.ps1']) sourceSha256[name] = createHash('sha256')
    .update(await fs.readFile(path.join(root, 'tools/probes/frame-video', name))).digest('hex');
  await fs.mkdir(path.dirname(output), { recursive: true });
  await fs.mkdir(counterDirectory); // Never mix runs.
  const stopFile = path.join(counterDirectory, 'stop');
  const sampler = sampled ? spawn('powershell.exe', ['-NoLogo', '-NoProfile', '-File',
    path.join(root, 'tools/probes/frame-video/gpu_memory.ps1'), '-TargetsFile', path.join(output, 'memory-targets.json'),
    '-OutputDirectory', counterDirectory], { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }) : null;
  const samplerLog = [];
  sampler?.stdout.on('data', b => samplerLog.push(b.toString()));
  sampler?.stderr.on('data', b => samplerLog.push(b.toString()));
  let child, timedOut = false, exitCode, timer, childError, samplerError;
  sampler?.on('error', error => { samplerError = String(error); });
  const samplerClosed = sampler ? new Promise(resolve => sampler.once('close', resolve)) : Promise.resolve();
  try {
    const readyDeadline = Date.now() + 20000;
    while (sampled) {
      try { await fs.access(path.join(counterDirectory, 'ready.json')); break; } catch {}
      if (samplerError || sampler.exitCode !== null || Date.now() > readyDeadline) throw Error('GPU counter sampler failed: ' + (samplerError || samplerLog.join('')));
      await delay(100);
    }
    if (sampled) await delay(4000);
    const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
    child = spawn(values.electron, [path.resolve(values.probe), ...pass, ...(sampled ? ['--gpu-memory'] : []), '--output', output],
      { cwd: path.resolve(path.dirname(values.probe), '../..'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const stdout = await fs.open(path.join(counterDirectory, 'probe.stdout.log'), 'wx');
    const stderr = await fs.open(path.join(counterDirectory, 'probe.stderr.log'), 'wx');
    let writes = Promise.resolve();
    child.stdout.on('data', bytes => { writes = writes.then(() => stdout.write(bytes)); });
    child.stderr.on('data', bytes => { writes = writes.then(() => stderr.write(bytes)); });
    child.on('error', error => { childError = String(error); });
    const durationIndex = pass.indexOf('--duration-seconds');
    const seconds = durationIndex >= 0 ? Number(pass[durationIndex + 1]) : 60;
    timer = setTimeout(() => {
      timedOut = true;
      if (child.pid) {
        const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
        killer.on('error', () => child.kill());
      }
    }, ((Number.isFinite(seconds) ? seconds : 60) + 180) * 1000);
    exitCode = await new Promise(resolve => child.once('close', resolve));
    clearTimeout(timer); await writes; await stdout.close(); await stderr.close();
    if (sampled) await delay(6000); // Observe counter disappearance and whole-adapter release.
  } finally {
    clearTimeout(timer);
    if (sampler) {
      await fs.writeFile(stopFile, '');
      await Promise.race([samplerClosed, delay(5000)]);
      if (sampler.exitCode === null) sampler.kill();
    }
    await fs.writeFile(path.join(counterDirectory, 'sampler.log'), samplerLog.join(''));
  }
  const report = { exitCode, timedOut, childError, samplerError, samplerExitCode: sampler?.exitCode ?? null, samplerDisabled: !sampled, sourceSha256,
    probe: path.relative(root, values.probe), arguments: pass,
    limitation: 'Per-process GPU memory may include shared allocations. Do not sum it as physical card usage; compare adapter counters separately.' };
  await fs.writeFile(path.join(counterDirectory, 'launcher.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ output, ...report }));
  process.exitCode = exitCode === 0 && !timedOut && !samplerError && (!sampled || sampler.exitCode === 0) ? 0 : 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
