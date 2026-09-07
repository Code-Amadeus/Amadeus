// Uses the same encrypted settings loader as the desktop. Secrets stay in the
// child environment; only translation text or an error category is printed.
const { app } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '../..');
app.setPath('userData', path.join(root, '.electron-user-data'));
app.whenReady().then(async () => {
  const { DesktopSettingsStore } = await import(pathToFileURL(path.join(root, 'electron/dist/main/desktopSettings.js')).href);
  const { resolvePythonCommand } = await import(pathToFileURL(path.join(root, 'electron/dist/main/pythonRuntime.js')).href);
  const settings = new DesktopSettingsStore(path.join(root, '.electron-user-data/settings.json'), path.join(root, '.env'));
  const fileIndex = process.argv.indexOf('--sample-file');
  const sample = fileIndex >= 0 ? JSON.parse(fs.readFileSync(process.argv[fileIndex + 1], 'utf8')) : process.argv.includes('--samples')
    ? {text: '演示网站的布局检查已完成，图片说明仍需要用户确认。', ended: true, project_name: '演示网站', provider: 'Codex', count: 3}
    : {text: '现在这段日语提醒的音量和语速合适吗？', provider: 'Codex', count: 1};
  const child = spawn(resolvePythonCommand({projectRoot: root}), ['-c',
    "import asyncio,json,sys,time\nfrom server.vn_tts_bridge import translate_notification,stream_notification,submit_vn_tts_confirmed\nasync def main():\n sample=json.loads(sys.argv[1]); count=sample.pop('count',1); recent=[]\n for index in range(count):\n  try:\n   started=time.perf_counter(); receipt=None\n   if sys.argv[2]=='enqueue':\n    pending=asyncio.Queue()\n    receipt=await submit_vn_tts_confirmed({'display_text':sample['text'],'complete_turn':True},pending_sentence_items=pending,voice_stream=stream_notification(**sample,recent_spoken=recent))\n    pieces=[]\n    while not pending.empty(): pieces.append(pending.get_nowait().text)\n    text=''.join(pieces)\n   else: text=await translate_notification(**sample,recent_spoken=recent)\n   print(json.dumps({'sample':index+1,'japanese':text,'generation_ms':round((time.perf_counter()-started)*1000),'enqueue_receipt':receipt,'audio_played':False},ensure_ascii=False),flush=True)\n   recent.append(text[:180])\n  except Exception as e:\n   print(json.dumps({'error_type':type(e).__name__,'status':getattr(e,'status_code',None)},ensure_ascii=False),flush=True)\nasyncio.run(main())", JSON.stringify(sample), process.argv.includes('--confirm-enqueue') ? 'enqueue' : 'translate'],
    {cwd: root, windowsHide: true, env: {...settings.backendEnvironment(process.env), ...process.env, PYTHONIOENCODING: 'utf-8'}, stdio:['ignore','pipe','pipe']});
  child.stdout.pipe(process.stdout);
  child.stderr.on('data', () => {});
  child.on('exit', code => app.exit(code || 0));
});
