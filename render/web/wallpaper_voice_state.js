(function (global) {
  "use strict";

  function initial() {
    return { phase: "idle", active: false, userText: "", deadlineMs: 0, error: "" };
  }

  function reduce(previous, payload) {
    const current = previous || initial();
    const value = payload && typeof payload === "object" ? payload : {};
    if (value.source && value.source !== "wake") return current;
    const status = String(value.status || "").toLowerCase();
    if (["idle", "unloaded"].includes(status)) return initial();
    if (status === "error") {
      return { ...current, active: true, phase: "error", deadlineMs: 0, error: String(value.error || "语音不可用") };
    }
    if (["awake", "listening", "no_speech", "turn_complete"].includes(status)) {
      return { ...current, active: true, phase: "listening", deadlineMs: Number(value.awake_deadline_ms) || 0, error: "" };
    }
    if (status === "recognized") {
      return { ...current, active: true, phase: "recognized", userText: String(value.text || ""), deadlineMs: 0, error: "" };
    }
    if (["thinking", "waiting_turn_complete"].includes(status)) {
      return { ...current, active: true, phase: "thinking", userText: String(value.text || current.userText), deadlineMs: 0, error: "" };
    }
    return current;
  }

  function remainingSeconds(state, nowMs) {
    if (!state || state.phase !== "listening" || !(state.deadlineMs > 0)) return null;
    return Math.max(0, Math.ceil((state.deadlineMs - nowMs) / 1000));
  }

  global.AmadeusWallpaperVoiceState = { initial, reduce, remainingSeconds };
})(window);
