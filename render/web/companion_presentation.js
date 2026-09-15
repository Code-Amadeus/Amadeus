(function (root) {
  "use strict";
  const aliases = { idle: "normal", idle1: "normal", idle2: "normal", neutral: "normal", thinking: "sided_thinking", thinking_trans: "sided_thinking", serious_speaking: "sided_thinking", speaking_trans: "sided_thinking", surprise: "sided_surprised", surprise_trans: "sided_surprised", surprised: "sided_surprised", smile: "happy", trans_smile: "happy", shy: "blush", shy_trans: "blush", angry_trans: "angry", sad_trans: "sad" };
  function apply(state, call) {
    const value = call && call.args && call.args[0];
    switch (call && call.method) {
      case "setSubtitle": return String(value || "").trim() ? { ...state, text: String(value) } : state;
      case "setEmotion": return { ...state, emotion: aliases[value] || String(value || "normal") };
      case "triggerSpriteForgeIntent": return aliases[value] ? { ...state, emotion: aliases[value] } : state;
      case "setSpeaking": return { ...state, speaking: value === true };
      default: return state;
    }
  }
  root.CompanionPresentation = { apply };
})(typeof globalThis !== "undefined" ? globalThis : this);
