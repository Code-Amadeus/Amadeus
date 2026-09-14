(function () {
  "use strict";
  const api = window.companion;
  const caption = document.getElementById("caption");
  const status = document.getElementById("status");
  const portrait = document.getElementById("portrait");
  const fallback = document.getElementById("fallback");
  let frames = {};
  let state = { text: "", emotion: "normal", speaking: false };
  let connected = false;
  let frameIndex = 0;
  let source = null;
  function paint() {
    const text = connected ? (state.text || "我在这里，继续吧。") : "连接已断开，正在重连…";
    if (caption.textContent !== text) { caption.textContent = text; caption.scrollTop = 0; }
    status.textContent = connected ? (state.speaking ? "SPEAKING" : "STANDBY") : "RECONNECTING";
    document.body.classList.toggle("speaking", connected && state.speaking);
  }
  document.getElementById("close").onclick = () => { void api?.close(); };
  document.getElementById("dock").onclick = async () => {
    const docked = await api?.dock();
    if (!docked) status.textContent = "请先用 W 打开游戏预览";
  };
  const animation = setInterval(() => {
    const emotion = frames[state.emotion] || frames.normal;
    if (!emotion) return;
    const mode = connected && state.speaking ? "speaking" : "idle";
    const sequence = emotion[mode]?.length ? emotion[mode] : emotion.idle;
    if (!sequence?.length) return;
    portrait.src = sequence[frameIndex++ % sequence.length];
    portrait.hidden = false;
    fallback.hidden = true;
  }, 180);
  async function start() {
    frames = await api?.portraits() || {};
    const port = Number(new URLSearchParams(location.search).get("bridgePort"));
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Missing presentation bridge");
    source = new EventSource(`http://127.0.0.1:${port}/wallpaper/events`);
    source.onopen = () => {
      connected = true;
      paint();
      void api?.connected(true);
    };
    source.onerror = () => {
      connected = false;
      state = { text: "", emotion: "normal", speaking: false };
      paint();
      void api?.connected(false);
    };
    source.onmessage = event => {
      try {
        const next = window.CompanionPresentation.apply(state, JSON.parse(event.data));
        if (next !== state) { state = next; paint(); }
      } catch (error) { console.warn("[companion] invalid presentation event", error); }
    };
  }
  window.addEventListener("beforeunload", () => { clearInterval(animation); source?.close(); });
  start().catch(error => { caption.textContent = "面板暂时无法连接，可以关闭后重新打开。"; console.error(error); });
})();
