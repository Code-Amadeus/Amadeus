(function () {
  "use strict";
  const api = window.companion;
  const caption = document.getElementById("caption");
  const status = document.getElementById("status");
  let portrait = document.getElementById("portrait");
  const fallback = document.getElementById("fallback");
  const motion = document.getElementById("motion");
  let atlas = null;
  let returnTimer = null;
  let staticIdle = false;
  try { staticIdle = localStorage.getItem('companionStaticIdle') === 'true'; } catch { /* optional preference */ }
  let frames = {};
  let state = { text: "", emotion: "normal", speaking: false };
  let connected = false;
  let frameIndex = 0;
  let source = null;
  function paint() {
    const text = connected ? (state.text || "我在这里，继续吧。") : "连接已断开，正在重连…";
    if (caption.textContent !== text) { caption.textContent = text; caption.scrollTop = 0; }
    status.textContent = connected ? (state.speaking ? "VOICE" : "STANDBY") : "RECONNECTING";
    document.body.classList.toggle("speaking", connected && state.speaking);
  }
  function portraitError(error) {
    portrait.hidden = true;
    fallback.hidden = false;
    fallback.title = '立绘加载失败，请检查 Companion Lite 资源包';
    console.error('[companion] portrait unavailable', error);
  }
  async function paintPortrait() {
    portrait.dataset.emotion = state.emotion;
    if (!atlas) return;
    try {
      await atlas.select(state.emotion, connected && state.speaking, staticIdle);
      portrait.hidden = false;
      fallback.hidden = true;
    } catch (error) { portraitError(error); }
  }
  function cancelReturn() { clearTimeout(returnTimer); returnTimer = null; }
  motion.onclick = () => {
    staticIdle = !staticIdle;
    try { localStorage.setItem('companionStaticIdle', String(staticIdle)); } catch { /* optional preference */ }
    motion.textContent = staticIdle ? '静' : '动';
    motion.setAttribute('aria-pressed', String(!staticIdle));
    void paintPortrait();
  };
  function visibility() {
    atlas?.setPaused(document.hidden);
    document.body.classList.toggle('presentation-paused', document.hidden);
  }
  document.addEventListener('visibilitychange', visibility);
  document.getElementById("close").onclick = () => { void api?.close(); };
  document.getElementById("dock").onclick = async () => {
    const docked = await api?.dock();
    if (!docked) status.textContent = "请先用 W 打开游戏预览";
  };
  const animation = setInterval(() => {
    if (atlas || document.hidden) return;
    const emotion = frames[state.emotion] || frames.normal;
    if (!emotion) return;
    const mode = connected && state.speaking ? "speaking" : "idle";
    const sequence = emotion[mode]?.length ? emotion[mode] : emotion.idle;
    if (!sequence?.length) return;
    portrait.src = sequence[frameIndex++ % sequence.length];
    portrait.hidden = false;
    fallback.hidden = true;
  }, 170);
  async function start() {
    frames = await api?.portraits() || {};
    if (!Object.keys(frames).length) {
      const base = new URL('/assets/companion/kurisu/', location.href);
      try {
        const response = await fetch(new URL('manifest.json', base));
        if (response.ok) {
          const manifest = window.CompanionAtlas.validate(await response.json());
          const canvas = document.createElement('canvas');
          canvas.id = 'portrait';
          canvas.setAttribute('role', 'img');
          canvas.setAttribute('aria-label', '牧濑红莉栖');
          portrait.replaceWith(canvas);
          portrait = canvas;
          atlas = new window.CompanionAtlas.Player(canvas, manifest, base);
          clearInterval(animation);
          motion.hidden = false;
          motion.textContent = staticIdle ? '静' : '动';
          motion.setAttribute('aria-pressed', String(!staticIdle));
          visibility();
          await paintPortrait();
        } else if (response.status === 404) {
          fallback.title = '未安装 Companion Lite 立绘包';
        } else throw new Error(`Portrait manifest HTTP ${response.status}`);
      } catch (error) { portraitError(error); }
    }
    const port = Number(new URLSearchParams(location.search).get("bridgePort"));
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Missing presentation bridge");
    source = new EventSource(`http://127.0.0.1:${port}/wallpaper/events?retainSubtitle=true`);
    source.onopen = () => {
      connected = true;
      paint();
      void paintPortrait();
      void api?.connected(true);
    };
    source.onerror = () => {
      connected = false;
      cancelReturn();
      state = { text: "", emotion: "normal", speaking: false };
      paint();
      void paintPortrait();
      void api?.connected(false);
    };
    source.onmessage = event => {
      try {
        const call = JSON.parse(event.data);
        const next = window.CompanionPresentation.apply(state, call);
        if (next !== state) {
          const changed = next.emotion !== state.emotion || next.speaking !== state.speaking;
          const speechEnded = state.speaking && !next.speaking;
          if (next.speaking || call.method === 'setEmotion' || call.method === 'triggerSpriteForgeIntent') cancelReturn();
          if (changed) frameIndex = 0;
          state = next;
          paint();
          if (changed) void paintPortrait();
          if (speechEnded && state.emotion !== 'normal') {
            cancelReturn();
            returnTimer = setTimeout(() => {
              returnTimer = null;
              state = { ...state, emotion: 'normal' };
              frameIndex = 0;
              void paintPortrait();
            }, 350);
          }
        }
      } catch (error) { console.warn("[companion] invalid presentation event", error); }
    };
  }
  window.addEventListener("beforeunload", () => { clearInterval(animation); cancelReturn(); source?.close(); atlas?.dispose(); });
  start().catch(error => { caption.textContent = "面板暂时无法连接，可以关闭后重新打开。"; console.error(error); });
})();
