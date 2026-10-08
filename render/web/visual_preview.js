(function () {
  "use strict";
  const app = new PIXI.Application({
    resizeTo: window, backgroundAlpha: 0, antialias: true,
    resolution: Math.min(window.devicePixelRatio || 1, 2), autoDensity: true,
  });
  app.ticker.maxFPS = 30;
  document.body.appendChild(app.view);
  const label = document.getElementById("state");
  const character = window.createModelCharacter("live2d", app, status => {
    label.textContent = status.error || status.state;
    window.parent.postMessage({ type: "amadeus.visual.preview.status", status }, "*");
  });
  app.stage.addChild(character.container);
  character.setPaused(document.hidden);
  app.renderer.on("resize", () => character.setViewport(null));
  document.addEventListener("visibilitychange", () => character.setPaused(document.hidden));
  window.addEventListener("message", event => {
    if (event.source !== window.parent || event.data?.type !== "amadeus.visual.preview") return;
    const message = event.data;
    if (message.action === "configure") character.configure(message.config || {});
    else if (message.action === "intent") character.applyIntent(message.label);
    else if (message.action === "expression") character.applyExpression(message.name);
    else if (message.action === "mouth") {
      character.setSpeaking(Number(message.value) > 0);
      character.setMouth(message.value);
    } else if (message.action === "reset") {
      character.setSpeaking(false);
      character.release();
    } else if (message.action === "destroy") character.destroy();
  });
  window.addEventListener("unload", () => {
    if (!character.container.destroyed) character.destroy();
    app.destroy(true, { children: true });
  }, { once: true });
  // The listener and runtime exist before the parent receives this handshake.
  window.parent.postMessage({ type: "amadeus.visual.preview.ready" }, "*");
})();
