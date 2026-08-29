"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const rendererPath = process.argv[2];
const source = fs.readFileSync(rendererPath, "utf8");
const start = source.indexOf("class SpriteForgeRuntime {");
const end = source.indexOf("\n  // Live2DRenderer", start);
assert.ok(start >= 0 && end > start, "SpriteForgeRuntime source must be extractable");
vm.runInThisContext(
  `${source.slice(start, end)}\nglobalThis.SpriteForgeRuntime = SpriteForgeRuntime;`,
  { filename: rendererPath },
);

function buildRuntime(holdSec = 0.01) {
  const calls = [];
  const sprite = {
    clearSpeculativeFrameLoads() {},
    clearHold() { calls.push("clearHold"); },
    holdClosedFrame() { calls.push("holdClosedFrame"); },
    holdFrame(which) { calls.push(["holdFrame", which]); },
    setEmotion(label) { calls.push(["emotion", label]); },
    setSpeaking(value) { calls.push(["speaking", value]); },
  };
  const runtime = new globalThis.SpriteForgeRuntime(sprite);
  runtime.graph = {
    nodes: [
      { id: "idle", label: "idle" },
      { id: "speaking", label: "speaking_short" },
      { id: "thinking", label: "thinking" },
    ],
    edges: [],
  };
  runtime.nodesById = Object.fromEntries(runtime.graph.nodes.map((node) => [node.id, node]));
  runtime.labelToIds = { idle: ["idle"], speaking_short: ["speaking"], thinking: ["thinking"] };
  runtime.rootNodeId = "idle";
  runtime.currentNodeId = "speaking";
  runtime.speechActive = true;
  runtime.cfg = {
    speakingReleaseLabels: ["speaking_short"],
    nonEmotionSpeakingLabels: ["speaking_short"],
    postSpeechHoldSec: holdSec,
  };
  return { runtime, calls };
}

async function wait(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function main() {
  {
    const { runtime, calls } = buildRuntime();
    runtime.release({ presentation_handoff: "after_speech" });
    const firstHoldTimer = runtime.postSpeechTimer;
    assert.equal(runtime.currentNodeId, "speaking");
    assert.equal(runtime.postSpeechHoldActive, true);
    assert.ok(calls.includes("holdClosedFrame"));
    runtime.setSpeaking(false);
    assert.equal(runtime.postSpeechTimer, firstHoldTimer, "duplicate false must not extend the hold");
    await wait(30);
    assert.equal(runtime.currentNodeId, "idle");
  }

  {
    const { runtime, calls } = buildRuntime();
    runtime.release({ presentation_handoff: "immediate" });
    assert.equal(runtime.currentNodeId, "idle");
    assert.equal(runtime.postSpeechHoldActive, false);
    assert.equal(calls.includes("holdClosedFrame"), false);
  }

  {
    const { runtime } = buildRuntime();
    runtime.trigger("thinking", { presentation_handoff: "after_speech" });
    assert.equal(runtime.currentNodeId, "speaking");
    assert.equal(runtime.deferredPresentationIntent, "thinking");
    await wait(30);
    assert.equal(runtime.currentNodeId, "thinking");
    assert.equal(runtime.deferredPresentationIntent, null);
  }

  {
    const { runtime } = buildRuntime();
    runtime.speechActive = false;
    runtime.currentNodeId = "thinking";
    runtime.release({ presentation_handoff: "after_speech" });
    assert.equal(runtime.currentNodeId, "idle", "a claim with no speech hold releases immediately");
  }

  {
    const { runtime } = buildRuntime();
    runtime.trigger("thinking", { presentation_handoff: "after_speech" });
    assert.equal(runtime.deferredPresentationIntent, "thinking");
    runtime.release({ presentation_handoff: "after_speech" });
    assert.equal(runtime.deferredPresentationIntent, null);
    await wait(30);
    assert.equal(runtime.currentNodeId, "idle", "a later empty claim set cancels a stale handoff");
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
