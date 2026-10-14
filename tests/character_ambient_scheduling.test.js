"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const quietConsole = { log() {}, info() {}, warn() {} };

function characterRuntime() {
  let now = 0;
  const math = Object.create(Math);
  math.random = () => 0.999;
  const source = fs.readFileSync("render/web/renderer.js", "utf8");
  const start = source.indexOf("class SpriteForgeRuntime {");
  const end = source.indexOf("\n  // Character rendering adapters", start);
  assert.ok(start >= 0 && end > start);
  const Runtime = vm.runInNewContext(source.slice(start, end) + "\nSpriteForgeRuntime", {
    console: quietConsole, Math: math, Date: { now: () => now },
    setTimeout: (callback) => ({ callback }), clearTimeout() {},
  });
  const sprite = { clearHold() {}, setEmotion() {}, setSpeaking() {}, holdClosedFrame() {} };
  const runtime = new Runtime(sprite);
  runtime._prefetchLabels = () => {};
  const payload = {
    rootNodeId: "idle",
    graph: {
      nodes: [
        { id: "idle", label: "idle" }, { id: "variant", label: "idle1" },
        { id: "standby", label: "trans_standby" },
        { id: "side", label: "idle_side_wind" },
        { id: "speaking", label: "speaking_short" },
      ],
      edges: [
        { from: "idle", to: "idle", prob: 0.95 },
        { from: "idle", to: "standby", prob: 0.05 },
        { from: "variant", to: "variant", prob: 0.95 },
        { from: "variant", to: "standby", prob: 0.05 },
        { from: "standby", to: "side", prob: 1 },
        { from: "idle", to: "speaking", prob: 0 },
      ],
    },
    config: {
      idleStandbyEntryLabel: "trans_standby",
      idleStandbyDelaySec: 30, defaultSpeakingTriggerLabel: "speaking_short",
      speakingReleaseLabels: ["speaking_short"], nonEmotionSpeakingLabels: ["speaking_short"],
      postSpeechHoldSec: 1,
    },
  };
  runtime.loadGraph(payload);
  return { runtime, payload, at: (seconds) => { now = seconds * 1000; } };
}

function scenarioRuntime(payload = {}) {
  let now = 0;
  const math = Object.create(Math);
  math.random = () => 0;
  const source = fs.readFileSync("render/web/wallpaper_scene.js", "utf8");
  const start = source.indexOf("  const scenarioRuntime = {");
  const end = source.indexOf("  const desktopScene = {", start);
  assert.ok(start >= 0 && end > start);
  const scenario = vm.runInNewContext(source.slice(start, end) + "\nscenarioRuntime", {
    console: quietConsole, Math: math, Date: { now: () => now }, window: {}, diag() {},
    keyboardSfx: { configure() {}, tick() {} }, desktopScene: { _crtBounds: {} },
  });
  scenario.init(null, {
    enabled: true,
    graph: { nodes: [
      { id: "a", label: "rest" }, { id: "b", label: "reading" },
      { id: "work", label: "computer use" },
    ], edges: [] },
    resources: {
      a: { type: "image", url: "a.png" }, b: { type: "image", url: "b.png" },
      work: { type: "image", url: "work.png" },
    },
    ...payload,
  });
  scenario.container = { visible: true, alpha: 1 };
  scenario.sprite = {};
  scenario.active = true;
  scenario._savedVisibility = {};
  scenario._textureLoading = false;
  scenario._updateComputerUseSfx = () => {};
  scenario._tickFade = () => {};
  scenario._heartbeat = () => {};
  scenario._setTexture = (_url, ready) => { if (ready) ready(); return true; };
  scenario._chooseNext = () => "b";
  scenario._showNode("a");
  return { scenario, math, at: (seconds) => { now = seconds * 1000; } };
}

const cases = {
  idle_delay() {
    const { runtime, payload, at } = characterRuntime();
    at(29.9);
    runtime.onCycleComplete("idle");
    assert.equal(runtime.currentNodeId, "idle", "side entry needs a complete quiet period");
    runtime._playNode("variant");
    runtime.loadGraph(payload);
    at(30);
    runtime.onCycleComplete("idle1");
    assert.equal(runtime.currentNodeId, "standby", "idle variations and identical bootstrap preserve quiet time");
    runtime.onCycleComplete("trans_standby");
    assert.equal(runtime.currentNodeId, "side", "the authored side route still runs after entry");
  },
  speech_and_intent() {
    const { runtime, at } = characterRuntime();
    at(29);
    runtime.setSpeaking(true);
    assert.equal(runtime.currentNodeId, "speaking", "speech does not wait for ambient eligibility");
    at(40);
    runtime.setSpeaking(false);
    at(41);
    runtime.postSpeechTimer.callback();
    assert.equal(runtime.currentNodeId, "idle");
    at(50);
    runtime.setSpeaking(false);
    at(69.9);
    runtime.onCycleComplete("idle");
    assert.equal(runtime.currentNodeId, "idle", "the quiet wait starts at speech end");
    at(70);
    runtime.onCycleComplete("idle");
    assert.equal(runtime.currentNodeId, "standby");
    const manual = characterRuntime().runtime;
    manual.trigger("trans_standby");
    assert.equal(manual.currentNodeId, "standby", "explicit intents are not ambient timer decisions");
  },
  idle_scenario_clock() {
    const { scenario, at } = scenarioRuntime();
    scenario.active = false;
    scenario.activate = () => { scenario.active = true; };
    scenario.deactivate = () => { scenario.active = false; };
    at(59.9);
    scenario.tick(0.01);
    assert.equal(scenario.active, false);
    at(60);
    scenario.tick(0.01);
    assert.equal(scenario.active, true, "entry uses elapsed seconds, not rendered frame count");
    scenario.setSpeaking(true);
    assert.equal(scenario.active, false, "voice returns an idle background scene to the foreground");
    at(100);
    scenario.tick(0.01);
    assert.equal(scenario.active, false, "speaking cannot accrue quiet time");
    at(110);
    scenario.setSpeaking(false);
    at(150);
    scenario.setSpeaking(false);
    scenario.noteActivity();
    at(169.9);
    scenario.tick(0.01);
    assert.equal(scenario.active, false);
    at(170);
    scenario.tick(0.01);
    assert.equal(scenario.active, true, "duplicate stopped-state and text events do not restart the speech clock");
    scenario.setActivity("work");
    at(180);
    scenario.setSpeaking(true);
    at(190);
    scenario.setSpeaking(false);
    assert.equal(scenario.active, true, "existing background work rendering remains in place");
    scenario.setActivity(null);
    at(249.9);
    scenario.tick(0.01);
    assert.equal(scenario.active, false);
    at(250);
    scenario.tick(0.01);
    assert.equal(scenario.active, true, "background narration restarts the same one-minute wait");
    scenario.setActivity("work");
    at(260);
    scenario.setActivity(null);
    scenario.tick(0.01);
    assert.equal(scenario.active, true, "silent Work completion preserves elapsed time since speech");
  },
  static_cadence() {
    const { scenario, math } = scenarioRuntime();
    assert.equal(scenario.nodeHoldSeconds, 45);
    scenario.tick(44);
    assert.equal(scenario.currentNodeId, "a");
    scenario.tick(1);
    assert.equal(scenario.currentNodeId, "b");
    math.random = () => 0.5;
    scenario._showNode("a");
    assert.equal(scenario.nodeHoldSeconds, 52.5);
    math.random = () => 0;
    scenario.tick(45);
    assert.equal(scenario.currentNodeId, "a", "dwell is sampled once, not redrawn on each tick");
    scenario.tick(7.5);
    assert.equal(scenario.currentNodeId, "b");
    const fixed = scenarioRuntime({ staticHoldSeconds: 48 }).scenario;
    assert.equal(fixed.nodeHoldSeconds, 48, "an explicit legacy interval remains fixed");
  },
  animated_cadence() {
    const { scenario } = scenarioRuntime({
      staticHoldSeconds: 45.25, staticHoldMaxSeconds: 45.25, enableFramePlayback: true,
    });
    scenario.resources.a = { type: "frames", frames: ["a0.png", "a1.png"], fps: 2 };
    scenario._showNode("a");
    for (let i = 0; i < 181; i++) scenario.tick(0.25);
    assert.equal(scenario.currentNodeId, "a", "looping motion must not shorten the dwell");
    scenario.tick(0.25);
    assert.equal(scenario.currentNodeId, "a", "a due pose waits for a complete motion loop");
    scenario.tick(0.5);
    assert.equal(scenario.currentNodeId, "b");
  },
  activity_interrupt() {
    const { scenario } = scenarioRuntime();
    scenario.tick(2);
    scenario.setActivity("work");
    assert.equal(scenario.currentNodeId, "work", "work takes over without the ambient dwell");
    scenario.setSpeaking(true);
    assert.equal(scenario.currentNodeId, "work", "work remains visible during narration");
    const idle = scenarioRuntime().scenario;
    idle.deactivate = () => { idle.active = false; };
    idle.setSpeaking(true);
    assert.equal(idle.active, false, "ordinary speech interrupts the idle scenario immediately");
  },
};

assert.ok(cases[process.argv[2]], "a scheduling case is required");
cases[process.argv[2]]();
