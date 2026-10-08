(function () {
  "use strict";
  const api = window.companion;
  const caption = document.getElementById("caption");
  const status = document.getElementById("status");
  let portrait = document.getElementById("portrait");
  const fallback = document.getElementById("fallback");
  let atlas = null;
  let returnTimer = null;
  let frames = {};
  let state = { text: "", emotion: "normal", speaking: false };
  let connected = false;
  let inputConnection = 0;
  let frameIndex = 0;
  let source = null;
  const card = document.getElementById('card');
  const inputHint = document.getElementById('input-hint');
  const inputButtons = { voice: document.getElementById('voice'), vision: document.getElementById('vision') };
  let inputs = null, inputPending = false, inputStatusRequest = null, inputError = '', hoverInput = '', inputPoll = null;
  function renderInputs() {
    const otherVoice = inputs?.voice?.active && inputs.voice.source !== 'wake';
    for (const [name, button] of Object.entries(inputButtons)) {
      const selected = name === 'voice' ? Boolean(inputs?.voice?.active && !otherVoice) : inputs?.watching === true;
      const available = connected && inputs && (name === 'voice' ? !otherVoice : inputs.supports_images === true);
      const label = name === 'voice' ? '语音输入（持续通话）' : '持续观察（通用视觉）';
      const detail = inputPending ? '正在切换…' : !connected ? '连接已断开' : !inputs ? '输入控制暂不可用'
        : name === 'voice' && otherVoice ? '麦克风正忙' : !available ? '当前聊天模型不支持图片' : selected ? '已开启' : '已关闭';
      button.disabled = !available || inputPending;
      button.setAttribute('aria-pressed', String(selected));
      button.setAttribute('aria-busy', String(inputPending));
      button.title = `${label} · ${detail}${inputError ? ' · ' + inputError : ''}`;
    }
    inputHint.hidden = !hoverInput;
    if (hoverInput) inputHint.textContent = inputButtons[hoverInput].title;
  }
  function errorText(error) {
    const message = String(error.message || error);
    return message === 'already_listening' ? '麦克风正忙' : message === 'current_model_does_not_support_images'
      ? '当前聊天模型不支持图片' : message;
  }
  async function readInputs(connection = inputConnection) {
    // A status snapshot and its unsent click belong to one uninterrupted connection.
    const current = () => connected && connection === inputConnection;
    if (!current()) return null;
    try {
      const result = await api.input('status');
      if (!current()) return null;
      if (result.ok !== true) throw new Error(result.error || '输入控制暂不可用');
      return result;
    } catch (error) { if (current()) throw error; return null; }
  }
  function refreshInputs() {
    if (!connected || !api?.input || inputPending) return;
    if (inputStatusRequest) return inputStatusRequest;
    const connection = inputConnection;
    inputStatusRequest = readInputs().then(snapshot => {
      if (snapshot && !inputPending) inputs = snapshot;
    }).catch(error => {
      if (connected && !inputPending) { inputs = null; inputError = errorText(error); }
    }).finally(() => {
      inputStatusRequest = null; renderInputs();
      if (connection !== inputConnection) void refreshInputs();
    });
    return inputStatusRequest;
  }
  async function toggleInput(name) {
    if (inputPending || inputButtons[name].disabled) return;
    const connection = inputConnection;
    inputPending = true; inputError = ''; renderInputs();
    try {
      await inputStatusRequest;
      // Read the actual owner/state before choosing start or stop; no optimistic toggles.
      const snapshot = await readInputs(connection);
      if (!snapshot) return;
      if (name === 'voice' && snapshot.voice?.active && snapshot.voice.source !== 'wake') throw new Error('already_listening');
      if (name === 'vision' && snapshot.supports_images !== true) throw new Error('current_model_does_not_support_images');
      const action = name === 'vision' ? 'vision_toggle' : snapshot.voice?.active ? 'voice_stop' : 'voice_start';
      const result = await api.input(action);
      if (result.ok !== true) throw new Error(result.error || '输入切换失败');
    } catch (error) { if (connected && connection === inputConnection) inputError = errorText(error); }
    finally {
      if (connected) {
        try { inputs = await readInputs(); }
        catch (error) { inputs = null; if (!inputError) inputError = errorText(error); }
      }
      inputPending = false; renderInputs();
      if (connection !== inputConnection) void refreshInputs();
    }
  }
  for (const [name, button] of Object.entries(inputButtons)) {
    button.onclick = () => { void toggleInput(name); };
    if (api?.input) {
      for (const event of ['mouseenter', 'focus']) button.addEventListener(event, () => { hoverInput = name; renderInputs(); });
      for (const event of ['mouseleave', 'blur']) button.addEventListener(event, () => { hoverInput = ''; renderInputs(); });
    }
  }
  if (api?.input) {
    card.addEventListener('pointerenter', () => { void refreshInputs(); });
    card.addEventListener('focusin', () => { void refreshInputs(); });
    inputPoll = setInterval(() => {
      if (!document.hidden && card.matches(':hover, :focus-within')) void refreshInputs();
    }, 2000);
  }
  renderInputs();
  let contentHeight = 0;
  function fitCaption() {
    if (!api?.fitContent) return;
    // Measure the untruncated text, even after the Host caps the window to a screen.
    const height = Math.max(226, Math.ceil(caption.getBoundingClientRect().top + caption.scrollHeight + 36));
    if (height !== contentHeight) { contentHeight = height; void api.fitContent(height); }
  }
  const layout = new ResizeObserver(fitCaption);
  layout.observe(caption);
  document.fonts.ready.then(fitCaption);
  function paint() {
    const text = connected ? (state.text || "我在这里，继续吧。") : "连接已断开，正在重连…";
    if (caption.textContent !== text) { caption.textContent = text; caption.scrollTop = 0; fitCaption(); }
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
      await atlas.select(state.emotion, connected && state.speaking);
      portrait.hidden = false;
      fallback.hidden = true;
    } catch (error) { portraitError(error); }
  }
  function cancelReturn() { clearTimeout(returnTimer); returnTimer = null; }
  function visibility() {
    atlas?.setPaused(document.hidden);
    document.body.classList.toggle('presentation-paused', document.hidden);
  }
  document.addEventListener('visibilitychange', visibility);
  visibility();
  document.getElementById("close").onclick = () => { void api?.close(); };
  document.addEventListener('contextmenu', event => { event.preventDefault(); void api?.close(); });
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
      inputError = ''; renderInputs(); void refreshInputs();
      paint();
      void paintPortrait();
      void api?.connected(true);
    };
    source.onerror = () => {
      connected = false;
      inputConnection++;
      inputs = null; hoverInput = ''; renderInputs();
      cancelReturn();
      state = { text: "", emotion: "normal", speaking: false };
      paint();
      void paintPortrait();
      void api?.connected(false);
    };
    source.onmessage = event => {
      try {
        const call = JSON.parse(event.data);
        if (call.method === 'composerEvent' || call.method === 'setAsrStatus') void refreshInputs();
        const next = window.CompanionPresentation.apply(state, call, Object.keys(atlas?.manifest?.emotions || frames));
        if (next !== state) {
          const changed = next.emotion !== state.emotion || next.speaking !== state.speaking;
          const speechEnded = state.speaking && !next.speaking;
          if (next.speaking || call.method === 'setEmotion' || call.method === 'triggerCharacterIntent') cancelReturn();
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
  window.addEventListener("beforeunload", () => { layout.disconnect(); clearInterval(inputPoll); clearInterval(animation); cancelReturn(); source?.close(); atlas?.dispose(); });
  start().catch(error => { caption.textContent = "面板暂时无法连接，可以关闭后重新打开。"; fitCaption(); console.error(error); });
})();
