const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const budget = require('../render/web/render_budget.js');
const source = fs.readFileSync(path.join(__dirname, '../render/web/renderer.js'), 'utf8');
const start = source.indexOf('class SpriteRenderer {');
const end = source.indexOf('class SpriteForgeRuntime {', start);
const CANVAS = {canvasWidth: 764, canvasHeight: 1028};

class Display {
  constructor() {
    this.anchor = {set() {}};
    this.texture = {};
    this.scale = {value: null, set(value) { this.value = value; }};
    this.x = 0;
    this.y = 0;
  }
  addChild() {}
}

function renderer(screen = {width: 1280, height: 720}) {
  const ticker = {maxFPS: 60, deltaMS: 1000 / 60, add() {}};
  const context = {
    app: {ticker, screen}, renderBudget: budget.resolveRenderBudget({maxFps: 60, textureSampling: true}),
    PIXI: {Container: Display, Sprite: Display, Graphics: Display}, window: {RenderBudget: budget},
    console: {log() {}, warn() {}, error() {}}, setTimeout, clearTimeout,
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + '\nglobalThis.SpriteRenderer=SpriteRenderer;', context);
  return new context.SpriteRenderer(new Display());
}

function show(sprite, emotion, width, height) {
  sprite._currentEmotion = emotion;
  sprite.sprite.texture = {width, height};
  sprite._applyCurrentTransform();
  return {scale: sprite.sprite.scale.value, x: sprite.sprite.x, y: sprite.sprite.y};
}

test('a frame wider than the canvas keeps the character size and centre in a narrow viewport', () => {
  const sprite = renderer();
  sprite.setViewportBounds({x: 0, y: 0, width: 800, height: 800});
  sprite.setClipConfig('idle', {...CANVAS});
  sprite.setClipConfig('idle_closed_eye', {...CANVAS});
  const idle = show(sprite, 'idle', 764, 1028);
  const wide = show(sprite, 'idle_closed_eye', 960, 1028);
  assert.deepEqual(wide, idle);
});

test('a frame shorter than the canvas keeps the scale and stays aligned at the bottom', () => {
  const sprite = renderer();
  sprite.setViewportBounds({x: 0, y: 0, width: 1200, height: 800});
  sprite.setClipConfig('idle', {...CANVAS});
  sprite.setClipConfig('idle2', {loopMode: 'once_then_hold', frameIntervalMs: 21, ...CANVAS});
  assert.deepEqual(show(sprite, 'idle2', 764, 1026), show(sprite, 'idle', 764, 1028));
});

test('without a viewport the canvas height fills the screen', () => {
  const sprite = renderer({width: 1920, height: 1080});
  sprite.setClipConfig('idle_closed_eye', {...CANVAS});
  const wide = show(sprite, 'idle_closed_eye', 960, 1028);
  assert.equal(wide.scale, 1080 / 1028);
  assert.equal(wide.x, 960);
});

test('clips without a declared canvas are still fitted by their frames', () => {
  const sprite = renderer();
  sprite.setViewportBounds({x: 0, y: 0, width: 800, height: 800});
  const narrow = show(sprite, 'legacy', 764, 1028);
  const wide = show(sprite, 'legacy', 960, 1028);
  assert.equal(narrow.scale, 800 * 0.64 / 764);
  assert.equal(wide.scale, 800 * 0.64 / 960);
});
