(function () {
  "use strict";

  window.createWallpaperKeyboardComposer = function createWallpaperKeyboardComposer() {
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
    document.body.append(toggleRoot, composerRoot);

    const input = composerRoot.querySelector(".wallpaper-keyboard-composer-input");
    const form = composerRoot.querySelector(".wallpaper-keyboard-composer-form");
    const sendButton = composerRoot.querySelector(".wallpaper-keyboard-composer-send");
    const closeButton = composerRoot.querySelector(".wallpaper-keyboard-composer-close");
    const toggles = Array.from(toggleRoot.querySelectorAll("button"));
    const status = composerRoot.querySelector(".wallpaper-keyboard-composer-status");
    let bridgePort = "";
    let bridgeToken = "";
    let sending = false;
    let composerBounds = null;
    let initialLayoutPending = true;

    function updateSendState() {
      sendButton.disabled = sending || !String(input.value || "").trim();
    }

    function setExpanded(expanded) {
      composerRoot.hidden = !expanded;
      toggles.forEach(function (toggle) {
        toggle.setAttribute("aria-expanded", String(expanded));
      });
      if (expanded) {
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
      input.style.height = Math.round(threeLineHeight) + "px";
      composerRoot.style.height = Math.round(composerBounds.height) + "px";
      composerRoot.style.top = Math.round(composerBounds.y) + "px";
    }

    function layout(toggleBounds, nextComposerBounds) {
      if (!toggleBounds || !nextComposerBounds) return;
      composerBounds = nextComposerBounds;
      if (initialLayoutPending) {
        initialLayoutPending = false;
        setExpanded(true);
      }
      toggleRoot.hidden = false;
      toggleRoot.style.setProperty("--keyboard-toggle-left", Math.round(toggleBounds.x) + "px");
      toggleRoot.style.setProperty("--keyboard-toggle-top", Math.round(toggleBounds.y) + "px");
      toggleRoot.style.setProperty("--keyboard-toggle-width", Math.round(toggleBounds.width) + "px");
      toggleRoot.style.setProperty("--keyboard-toggle-height", Math.max(24, Math.round(toggleBounds.height)) + "px");
      composerRoot.style.setProperty("--keyboard-composer-left", Math.round(nextComposerBounds.x) + "px");
      composerRoot.style.setProperty("--keyboard-composer-width", Math.round(nextComposerBounds.width) + "px");
      composerRoot.style.setProperty("--keyboard-composer-max-height", Math.round(nextComposerBounds.height) + "px");
      if (!composerRoot.hidden) resizeInput();
    }

    async function submit() {
      const text = String(input.value || "").trim();
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
        const response = await fetch(
          "http://127.0.0.1:" + bridgePort + "/wallpaper/chat-action",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Amadeus-Bridge-Token": bridgeToken,
            },
            body: JSON.stringify({ text: text }),
          }
        );
        const result = await response.json().catch(function () { return {}; });
        if (!response.ok || (result.ok !== true && result.status !== "ok")) {
          throw new Error(String(result.error || "send_failed"));
        }
        input.value = "";
        resizeInput();
        updateSendState();
        status.textContent = "SENT";
        window.setTimeout(function () {
          if (!sending) status.textContent = "";
        }, 1200);
      } catch (error) {
        console.warn("[ElectronKeyboardComposer] send failed", error);
        status.textContent = "RETRY";
      } finally {
        sending = false;
        input.disabled = false;
        updateSendState();
        input.focus();
      }
    }

    toggles.forEach(function (toggle) {
      toggle.addEventListener("click", function () {
        setExpanded(composerRoot.hidden);
      });
    });
    closeButton.addEventListener("click", function () {
      setExpanded(false);
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
      },
      layout,
    };
  };
})();
