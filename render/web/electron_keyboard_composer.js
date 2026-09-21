(function () {
  "use strict";

  window.createWallpaperKeyboardComposer = function createWallpaperKeyboardComposer(options = {}) {
    const toggleRoot = document.createElement("section");
    toggleRoot.id = "wallpaper-keyboard-toggle";
    toggleRoot.hidden = true;
    toggleRoot.innerHTML = [
      '<button class="wallpaper-keyboard-composer-toggle crt-canvas-surface-status" type="button" aria-expanded="false">',
      '  <span>MESSAGE INPUT</span>',
      '</button>',
      '<button class="wallpaper-keyboard-composer-toggle-indicator crt-canvas-surface-dot" type="button" aria-label="Toggle text input" aria-expanded="false">',
      '  <span aria-hidden="true"></span>',
      '</button>',
    ].join("");

    const composerRoot = document.createElement("section");
    composerRoot.id = "wallpaper-keyboard-composer";
    composerRoot.hidden = true;
    composerRoot.setAttribute("aria-label", "Wallpaper text input");
    composerRoot.innerHTML = [
      '<div class="wallpaper-keyboard-composer-header">',
      '  <svg class="wallpaper-keyboard-composer-header-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.2 9.55 6.45 14.8 8 9.55 9.55 8 14.8 6.45 9.55 1.2 8 6.45 6.45Z" fill="currentColor"/><circle cx="8" cy="8" r="1.1" fill="#d9fff7"/></svg>',
      '  <strong>AMADEUS</strong><span>· MESSAGE</span>',
      '  <span class="wallpaper-keyboard-composer-status" role="status" aria-live="polite"></span>',
      '  <button class="wallpaper-keyboard-composer-close" type="button" aria-label="Close text input">×</button>',
      '</div>',
      '<form class="wallpaper-keyboard-composer-form">',
      '  <textarea class="wallpaper-keyboard-composer-input" rows="3" maxlength="8000" autocomplete="off" spellcheck="false" aria-label="Message Amadeus" placeholder="请输入你的消息…"></textarea>',
      '  <div class="wallpaper-keyboard-composer-footer">',
      '    <div class="wallpaper-keyboard-composer-hints">Enter 发送 · Shift + Enter 换行 · Esc 清空</div>',
      '    <button class="wallpaper-keyboard-composer-send" type="submit" aria-label="Send message" disabled>发送</button>',
      '  </div>',
      '</form>',
    ].join("");
    if (options.controls) {
      composerRoot.classList.add("has-chat-controls");
      toggleRoot.classList.add("has-chat-controls");
      composerRoot.querySelector(".wallpaper-keyboard-composer-header").innerHTML =
        '<strong>AMADEUS</strong><span class="wallpaper-keyboard-composer-status" role="status" aria-live="polite"></span>' +
        '<button class="composer-console composer-tool" type="button" title="打开控制台" aria-label="Open control panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.5 3h5l.5 2 1.6.9 2-.6 2.5 4.3-1.5 1.4v1.9l1.5 1.4-2.5 4.3-2-.6-1.6.9-.5 2h-5l-.5-2-1.6-.9-2 .6-2.5-4.3L4.4 13v-1.9L2.9 9.7l2.5-4.3 2 .6L9 5l.5-2Z"/><circle cx="12" cy="12" r="3"/></svg></button>' +
        '<button class="composer-new-chat composer-tool" type="button" title="新对话" aria-label="New chat"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-7M15 3l6 6M10 14l2-6 6-6a2 2 0 0 1 3 3l-6 6Z"/></svg></button>' +
        '<button class="wallpaper-keyboard-composer-close composer-tool" type="button" title="收起" aria-label="Close text input"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button>';
      composerRoot.querySelector(".wallpaper-keyboard-composer-send").innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>';
      composerRoot.querySelector("textarea").placeholder = "和 Amadeus 聊聊…";
      composerRoot.querySelector(".wallpaper-keyboard-composer-input").insertAdjacentHTML("afterend",
        '<div class="composer-attachment" hidden><img alt="待发送的图片"><span></span><button type="button" aria-label="Remove image">×</button></div>');
      composerRoot.querySelector(".wallpaper-keyboard-composer-hints").innerHTML =
        '<button class="composer-image composer-tool" type="button" aria-label="Attach image" title="添加图片" disabled><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/></svg></button>' +
        '<button class="composer-voice composer-tool" type="button" aria-label="Voice input" title="语音输入" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/></svg></button>' +
        '<span class="composer-voice-label">待唤醒</span><input class="composer-file" type="file" accept="image/*" hidden>';
    }
    if (options.controls) composerRoot.insertAdjacentHTML("beforeend",
      '<div class="composer-window-picker" role="dialog" aria-label="选择观察窗口" hidden></div>');
    document.body.append(toggleRoot, composerRoot);

    const input = composerRoot.querySelector(".wallpaper-keyboard-composer-input");
    const form = composerRoot.querySelector(".wallpaper-keyboard-composer-form");
    const sendButton = composerRoot.querySelector(".wallpaper-keyboard-composer-send");
    const closeButton = composerRoot.querySelector(".wallpaper-keyboard-composer-close");
    const toggles = Array.from(toggleRoot.querySelectorAll("button"));
    const status = composerRoot.querySelector(".wallpaper-keyboard-composer-status");
    const imageButton = composerRoot.querySelector(".composer-image");
    const voiceButton = composerRoot.querySelector(".composer-voice");
    const newChatButton = composerRoot.querySelector(".composer-new-chat");
    const fileInput = composerRoot.querySelector(".composer-file");
    const preview = composerRoot.querySelector(".composer-attachment");
    const windowPicker = composerRoot.querySelector(".composer-window-picker");
    let attachment = null;
    let supportsImages = false;
    let listening = false;
    let wakeRunning = false;
    let otherVoiceActive = false;
    let bridgePort = "";
    let bridgeToken = "";
    let sending = false;
    let composerBounds = null;
    let initialLayoutPending = true;

    function updateSendState() {
      sendButton.disabled = sending || (!String(input.value || "").trim() && !attachment);
      if (options.controls) closeButton.disabled = sending;
      if (imageButton) imageButton.disabled = sending || !supportsImages;
      if (voiceButton) voiceButton.disabled = sending || otherVoiceActive;
      if (newChatButton) newChatButton.disabled = sending;
    }

    function setExpanded(expanded) {
      composerRoot.hidden = !expanded;
      if (options.controls) toggleRoot.hidden = expanded;
      toggles.forEach(function (toggle) {
        toggle.setAttribute("aria-expanded", String(expanded));
      });
      if (expanded) {
        if (options.controls) void refreshCapabilities();
        resizeInput();
        window.setTimeout(function () { input.focus(); }, 0);
      }
    }

    function resizeInput() {
      if (!composerBounds) return;
      // Reset any inline value left by a previous layout, then restore the
      // authored fixed three-line composer footprint below.
      composerRoot.style.height = "auto";
      input.style.height = "auto";
      const style = window.getComputedStyle(input);
      const lineHeight = Number.parseFloat(style.lineHeight) || 16;
      const verticalPadding = (Number.parseFloat(style.paddingTop) || 0) + (Number.parseFloat(style.paddingBottom) || 0);
      const threeLineHeight = Math.max(
        lineHeight + verticalPadding,
        lineHeight * 3 + verticalPadding
      );
      input.style.height = Math.round(options.controls ? 42 : threeLineHeight) + "px";
      const height = options.controls ? Math.max(composerBounds.height, 124) + (attachment ? 42 : 0) : composerBounds.height;
      composerRoot.style.height = Math.round(height) + "px";
      composerRoot.style.top = Math.max(0, Math.round(composerBounds.y + composerBounds.height - height)) + "px";
    }

    function layout(toggleBounds, nextComposerBounds) {
      if (!toggleBounds || !nextComposerBounds) return;
      composerBounds = nextComposerBounds;
      if (initialLayoutPending) {
        initialLayoutPending = false;
        setExpanded(!options.startCollapsed);
      }
      toggleRoot.hidden = options.controls && !composerRoot.hidden;
      toggleRoot.style.setProperty("--keyboard-toggle-left", Math.round(toggleBounds.x) + "px");
      toggleRoot.style.setProperty("--keyboard-toggle-top", Math.round(toggleBounds.y) + "px");
      toggleRoot.style.setProperty("--keyboard-toggle-width", Math.round(toggleBounds.width) + "px");
      toggleRoot.style.setProperty("--keyboard-toggle-height", Math.max(24, Math.round(toggleBounds.height)) + "px");
      const width = options.controls ? Math.min(window.innerWidth - 24, nextComposerBounds.width * 1.15) : nextComposerBounds.width;
      const left = options.controls ? Math.max(12, Math.min(window.innerWidth - width - 12, nextComposerBounds.x + (nextComposerBounds.width - width) / 2)) : nextComposerBounds.x;
      composerRoot.style.setProperty("--keyboard-composer-left", Math.round(left) + "px");
      composerRoot.style.setProperty("--keyboard-composer-width", Math.round(width) + "px");
      composerRoot.style.setProperty("--keyboard-composer-max-height", Math.round(nextComposerBounds.height) + "px");
      if (!composerRoot.hidden) resizeInput();
    }

    async function submit() {
      const text = String(input.value || "").trim() || (attachment ? "请看这张图片。" : "");
      if (!text || sending) return;
      if (!bridgePort || !bridgeToken) {
        status.textContent = "CONNECTING";
        return;
      }
      sending = true;
      input.disabled = true;
      updateSendState();
      status.textContent = "SENDING";
      try {
        const payload = { text: text };
        if (attachment) payload.visual = {
          request: true, mode: "attachment", scope: "user_image", source: "user_image",
          attachment: { name: attachment.name, byteLength: attachment.byteLength },
          frame: { mime: attachment.mime, dataUrl: attachment.dataUrl, width: attachment.width,
            height: attachment.height, byteLength: attachment.byteLength },
        };
        await request(payload);
        setAttachment(null);
        input.value = "";
        resizeInput();
        updateSendState();
        status.textContent = "SENT";
        window.setTimeout(function () {
          if (!sending) status.textContent = "";
        }, 1200);
      } catch (error) {
        console.warn("[ElectronKeyboardComposer] send failed", error);
        showError(error);
      } finally {
        sending = false;
        input.disabled = false;
        updateSendState();
        input.focus();
      }
    }

    async function request(payload, keepalive = false) {
      if (!bridgePort || !bridgeToken) throw new Error("connecting");
      const response = await fetch("http://127.0.0.1:" + bridgePort + "/wallpaper/chat-action", {
        method: "POST", keepalive,
        headers: { "Content-Type": "application/json", "X-Amadeus-Bridge-Token": bridgeToken },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok || (result.ok !== true && result.status !== "ok")) {
        throw new Error(String(result.error || "action_failed"));
      }
      return result;
    }

    function showError(error) {
      status.textContent = error.message === "already_listening" ? "麦克风正忙" : "请重试";
      status.title = String(error.message || error);
    }

    function setAttachment(value) {
      attachment = value;
      if (preview) {
        preview.hidden = !value;
        preview.querySelector("img").src = value ? value.dataUrl : "";
        preview.querySelector("span").textContent = value ? value.name : "";
      }
      resizeInput();
      updateSendState();
    }

    async function refreshCapabilities() {
      try {
        const result = await request({ action: "status" });
        supportsImages = result.supports_images === true;
        imageButton.title = supportsImages ? "短按添加图片 · 长按切换持续观察 · 右键选择窗口" : "当前聊天模型不支持图片";
        imageButton.setAttribute("aria-pressed", String(result.watching === true));
        if (result.voice) {
          otherVoiceActive = result.voice.active && result.voice.source !== "wake";
          listening = result.voice.active && result.voice.source === "wake";
        }
        if (result.wake) wakeRunning = result.wake.running === true;
        renderVoiceState();
        updateSendState();
      } catch (error) { showError(error); }
    }

    function renderVoiceState() {
      const label = otherVoiceActive ? "麦克风正忙" : listening
        ? "持续通话 · 说完自动发送"
        : (wakeRunning ? "待唤醒 · 点击或唤醒词开始持续通话" : "唤醒未开启 · 短按开始对话");
      const voiceLabel = composerRoot.querySelector(".composer-voice-label");
      if (voiceLabel) voiceLabel.textContent = otherVoiceActive ? "麦克风正忙" : listening
        ? "持续通话 · 自动发送"
        : (wakeRunning ? "待唤醒" : "唤醒未开启");
      [voiceButton].forEach(function (button) {
        if (!button) return;
        button.title = label; button.setAttribute("aria-label", label);
        button.setAttribute("aria-pressed", String(listening));
        button.dataset.voiceState = listening ? "continuous" : "sleeping";
        let mark = button.querySelector("small");
        if (!mark) { mark = document.createElement("small"); button.append(mark); }
        mark.textContent = listening ? "∞" : (wakeRunning ? "Zz" : "–");
      });
      toggles.forEach(function (toggle) {
        toggle.title = label + " · 点击展开输入框";
        toggle.dataset.voiceState = listening ? "continuous" : "sleeping";
      });
      updateSendState();
    }

    async function control(payload) {
      sending = true; updateSendState();
      try { return await request(payload); }
      finally { sending = false; updateSendState(); }
    }

    // Pointer and keyboard activation share the short action; a completed long
    // press must never also open the file picker or toggle the microphone twice.
    function bindPress(button, shortAction, longAction) {
      let timer = 0;
      let pressed = false;
      let longPress = false;
      function cancel() { window.clearTimeout(timer); timer = 0; pressed = false; }
      function run(action) { Promise.resolve().then(action).catch(showError); }
      button.addEventListener("pointerdown", function (event) {
        if (event.button !== 0 || button.disabled) return;
        cancel(); pressed = true; longPress = false;
        button.setPointerCapture(event.pointerId);
        timer = window.setTimeout(function () { longPress = true; run(longAction); }, 560);
      });
      button.addEventListener("pointerup", function (event) {
        if (event.button !== 0 || !pressed) return;
        const wasLong = longPress; cancel();
        if (!wasLong) run(shortAction);
      });
      button.addEventListener("pointercancel", cancel);
      button.addEventListener("lostpointercapture", cancel);
      button.addEventListener("contextmenu", cancel);
      button.addEventListener("click", function (event) {
        if (event.detail === 0 && !button.disabled) run(shortAction);
      });
    }

    if (options.controls) {
      composerRoot.querySelector(".composer-console").addEventListener("click", async function () {
        try {
          if (!await window.amadeus?.focusMainWindow?.()) throw new Error("控制台暂不可用，请从托盘打开 Amadeus");
        } catch (error) { showError(error); }
      });
      bindPress(imageButton, function () { fileInput.click(); }, async function () {
        await control({ action: "vision_toggle" });
        setAttachment(null);
        await refreshCapabilities();
      });
      imageButton.addEventListener("contextmenu", async function (event) {
        event.preventDefault();
        if (imageButton.disabled) return;
        windowPicker.hidden = false;
        windowPicker.textContent = "正在读取窗口…";
        windowPicker.style.maxHeight = Math.max(90, Math.min(220, parseFloat(composerRoot.style.top) - 12)) + "px";
        try {
          const result = await request({ action: "vision_windows" });
          windowPicker.textContent = "";
          function addChoice(label, handler) {
            const button = document.createElement("button");
            button.type = "button"; button.textContent = label;
            button.addEventListener("click", handler); windowPicker.append(button);
          }
          addChoice("关闭窗口选择 ×", function () { windowPicker.hidden = true; });
          async function select(hwnd) {
            try {
              await control({ action: "vision_select", hwnd });
              setAttachment(null); windowPicker.hidden = true;
            } catch (error) { showError(error); }
          }
          addChoice("整个屏幕", function () { void select(""); });
          (result.windows || []).forEach(function (item) {
            if (item.hwnd && item.title) addChoice(String(item.title), function () { void select(String(item.hwnd)); });
          });
        } catch (error) { windowPicker.textContent = "无法读取窗口，请右键重试"; showError(error); }
      });
      fileInput.addEventListener("change", async function () {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        sending = true; updateSendState();
        try {
          const module = await import("./chat_image_attachment.mjs");
          setAttachment(await module.prepareImageAttachment(file));
          status.textContent = "";
        } catch (error) { showError(error); }
        finally { sending = false; fileInput.value = ""; updateSendState(); }
      });
      preview.querySelector("button").addEventListener("click", function () {
        if (!sending) setAttachment(null);
      });
      async function toggleVoice() {
        sending = true; updateSendState(); status.textContent = "连接语音…";
        try {
          const stop = listening;
          await request({ action: stop ? "voice_stop" : "voice_start" });
          await refreshCapabilities(); status.textContent = "";
        } catch (error) { showError(error); }
        finally { sending = false; updateSendState(); }
      }
      [voiceButton].forEach(function (button) {
        bindPress(button, toggleVoice, toggleVoice);
      });
      newChatButton.addEventListener("click", async function () {
        sending = true; updateSendState();
        try {
          await request({ action: "new_chat" });
          await refreshCapabilities();
          input.value = ""; setAttachment(null); status.textContent = "新对话";
          input.focus();
        } catch (error) { showError(error); }
        finally { sending = false; updateSendState(); }
      });
    }

    function onEvent(event) {
      if (!options.controls) return;
      const payload = event.params || {};
      if (event.method === "wake.status") {
        wakeRunning = payload.running === true;
      } else if (event.method === "asr.status") {
        if (payload.status === "idle" || payload.status === "error") {
          listening = false; otherVoiceActive = false;
        } else if (["awake", "listening", "loading", "paused_tts", "waiting_turn_complete"].includes(payload.status)) {
          listening = payload.source === "wake";
          otherVoiceActive = !listening;
        }
      }
      renderVoiceState();
    }

    toggles.forEach(function (toggle) {
      toggle.addEventListener("click", function () {
        if (sending && options.controls) return;
        setExpanded(composerRoot.hidden);
        if (composerRoot.hidden) {
          if (windowPicker) windowPicker.hidden = true;
        }
      });
    });
    closeButton.addEventListener("click", function () {
      setExpanded(false);
      if (windowPicker) windowPicker.hidden = true;
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      void submit();
    });
    input.addEventListener("input", function () {
      resizeInput();
      updateSendState();
    });
    input.addEventListener("keydown", function (event) {
      // Do not take keyboard shortcuts away from an IME while it is choosing
      // or committing a candidate.  keyCode 229 keeps the same behavior for
      // Chromium composition events that do not report isComposing reliably.
      if (event.isComposing || event.keyCode === 229) {
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        input.value = "";
        resizeInput();
        updateSendState();
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        void submit();
      }
    });

    return {
      configure(nextBridgePort, nextBridgeToken) {
        bridgePort = String(nextBridgePort || "");
        bridgeToken = String(nextBridgeToken || "");
        if (options.controls) void refreshCapabilities();
      },
      layout,
      onEvent,
    };
  };
})();
