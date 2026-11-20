/* Probe adapter: exact production card, subtitle/emotion projection and SSE signals. */
(async () => {
  try {
    const variant = new URLSearchParams(location.search).get('variant');
    const base = new URL(`/assets/${variant.replace('cpu', '')}/`, location.href);
    const manifest = await fetch(new URL('manifest.json', base)).then(r => r.json());
    const image = document.getElementById('portrait');
    const canvas = document.createElement('canvas');
    canvas.id = 'portrait';
    canvas.style.cssText = 'width:100%;height:100%;object-fit:contain';
    image.replaceWith(canvas);
    document.getElementById('fallback').hidden = true;
    const player = window.atlasPlayer = new CompanionAtlasPlayer(canvas, manifest, base,
      {byteLimit: (variant.startsWith('full264') ? 48 : 16) * 1024 * 1024, softwareCanvas:variant.endsWith('cpu')});
    let state = { text: '', emotion: 'normal', speaking: false };
    let connected = false;
    async function paint() {
      document.getElementById('caption').textContent = connected ? state.text || '我在这里，继续吧。' : '连接已断开，正在重连…';
      document.getElementById('status').textContent = connected ? state.speaking ? 'VOICE' : 'STANDBY' : 'RECONNECTING';
      document.body.classList.toggle('speaking', connected && state.speaking);
      await player.select(state.emotion, connected && state.speaking);
    }
    // OS occlusion is not universally exposed by visibilitychange; the native host
    // must signal explicit hidden/minimized state when integrating this experiment.
    window.probePause = paused => {
      player.setPaused(paused);
      document.querySelectorAll('.signal i').forEach(el => el.style.animationPlayState = paused ? 'paused' : 'running');
    };
    const source = new EventSource('/wallpaper/events?retainSubtitle=true');
    source.onopen = () => { connected = true; void paint(); };
    source.onerror = () => { connected = false; state = {text:'',emotion:'normal',speaking:false}; void paint(); };
    source.onmessage = event => {
      state = CompanionPresentation.apply(state, JSON.parse(event.data));
      paint().catch(error => { window.probeError = String(error.stack); });
    };
    window.addEventListener('beforeunload', () => { source.close(); player.dispose(); });
    await paint();
    window.probeReady = true;
  } catch (error) { window.probeError = String(error.stack); }
})();
