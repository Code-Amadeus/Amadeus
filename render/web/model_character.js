/** One explicit model-adapter construction boundary; no registry or plugin host. */
(function (root) {
  "use strict";
  root.createModelCharacter = function (kind, app, onStatus) {
    if (kind === "live2d") return new root.Live2DCharacter(app, onStatus);
    throw new Error("Unsupported model renderer: " + kind);
  };
})(typeof window !== "undefined" ? window : globalThis);
