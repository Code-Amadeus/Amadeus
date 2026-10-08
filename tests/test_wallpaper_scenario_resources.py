"""Scenario textures are released across character-backend changes."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('render/web/wallpaper_scene.js', 'utf8');
const start = source.indexOf('  const scenarioRuntime = {');
const end = source.indexOf('  const desktopScene = {', start);
assert.ok(start >= 0 && end > start);
const globalCache = new Map(), textures = [], images = [], requests = [], revoked = [];
class Texture {
  constructor(baseTexture = {valid: true, width: 100, height: 80}, frame) {
    this.baseTexture = baseTexture; this.frame = frame;
    this.width = 100; this.height = 80; this.destroyCalls = [];
    textures.push(this);
  }
  destroy(destroyBase) {
    this.destroyCalls.push(destroyBase);
    if (destroyBase) this.baseTexture.destroyed = true;
    for (const [key, value] of globalCache) if (value === this) globalCache.delete(key);
  }
  static from(image) {
    assert.ok(image instanceof Image, 'scenario loads own an image, not the shared URL texture');
    const texture = new Texture();
    globalCache.set('image-' + images.indexOf(image), texture);
    return texture;
  }
  static removeFromCache(key) {
    const texture = globalCache.get(key);
    globalCache.delete(key);
    return texture;
  }
}
Texture.EMPTY = {empty: true};
class Image {
  constructor() { images.push(this); this.naturalWidth = 100; this.naturalHeight = 80; }
}
const scope = {
  PIXI: {Texture, Rectangle: class {constructor(...values) {this.values = values;}}},
  Image, AbortController, setTimeout, clearTimeout,
  fetch(url, options) { return new Promise(resolve => requests.push({url, signal: options.signal, resolve})); },
  URL: {createObjectURL() {return 'blob:' + requests.length;}, revokeObjectURL(url) {revoked.push(url);}},
  desktopScene: {_crtBounds: {}}, diag() {}, console: {info() {}, warn() {}},
};
const scenario = vm.runInNewContext(source.slice(start, end) + '\nscenarioRuntime', scope);
const sharedUrl = 'https://assets.invalid/shared-background.png';
const background = new Texture();
globalCache.set(sharedUrl, background);
Object.assign(scenario, {
  configuredEnabled: true, enabled: true,
  sprite: {texture: Texture.EMPTY, visible: false},
  container: {visible: false, alpha: 0},
  backplateSprite: {texture: background},
  _textureCache: new Map(),
  _updateComputerUseSfx() {}, _restoreCharacter() {}, _saveAndHideCharacter() {},
  _placeSprite() {}, _heartbeat() {},
  graph: {nodes: [{id: 'work', label: 'computer use'}], edges: []},
  resources: {work: {type: 'image', url: sharedUrl}},
});
const flush = () => new Promise(resolve => setImmediate(resolve));
async function fetchReady(index) {
  requests[index].resolve({ok: true, status: 200, blob: async () => ({size: 10, type: 'image/png'})});
  await flush();
}
function finishImage(index) { images[index].onload(); }
function assertBackground() {
  assert.equal(globalCache.get(sharedUrl), background);
  assert.equal(scenario.backplateSprite.texture, background);
  assert.deepEqual(background.destroyCalls, []);
  assert.equal(background.baseTexture.destroyed, undefined);
}
async function main() {
  const mode = process.argv[1];
  if (mode === 'release-and-reactivate') {
    scenario.sourceCropNorm = [0, 0, 0.5, 0.5];
    scenario._setTexture(sharedUrl);
    await fetchReady(0); finishImage(0);
    const cropped = scenario._textureCache.get(sharedUrl);
    scenario.sourceCropNorm = null;
    scenario._setTexture('https://assets.invalid/second.png');
    await fetchReady(1); finishImage(1);
    const plain = scenario.sprite.texture;
    scenario.setCharacterBackend('live2d');
    assert.equal(scenario._textureCache.size, 0);
    assert.equal(scenario.sprite.texture, Texture.EMPTY);
    assert.equal(scenario.sprite.visible, false);
    assert.equal(scenario.container.visible, false);
    assert.deepEqual(cropped.texture.destroyCalls, [false]);
    assert.deepEqual(cropped.sourceTexture.destroyCalls, [true]);
    assert.deepEqual(plain.destroyCalls, [true]);
    assert.ok(revoked.includes('blob:1') && revoked.includes('blob:2'));
    assertBackground();
    scenario.setCharacterBackend('live2d');
    assert.deepEqual(plain.destroyCalls, [true], 'repeated disable must not release twice');
    scenario.setCharacterBackend('sprite');
    scenario.activate(scenario.graph.nodes[0]);
    await fetchReady(2); finishImage(2);
    assert.equal(scenario.active, true);
    assert.equal(scenario.container.visible, true);
    assert.equal(scenario.currentNodeId, 'work');
    assert.equal(scenario._textureCache.size, 1);
    assert.notEqual(scenario.sprite.texture, cropped.texture);
    assert.deepEqual(scenario.sprite.texture.destroyCalls, []);
    assertBackground();
  } else {
    let staleReady = 0;
    scenario._setTexture(sharedUrl, () => staleReady++);
    const oldSignal = requests[0].signal;
    if (mode === 'late-image') await fetchReady(0);
    scenario.setCharacterBackend('live2d');
    scenario.setCharacterBackend('sprite');
    scenario.activate(scenario.graph.nodes[0]);
    if (mode === 'late-fetch') {
      assert.equal(oldSignal.aborted, true);
      await fetchReady(0);
      assert.equal(images.length, 0, 'late body must not create another image or blob URL');
      assert.equal(scenario._pendingFetchController.signal, requests[1].signal);
    } else {
      finishImage(0);
      assert.ok(revoked.includes('blob:1'));
      assert.equal(textures.length, 1, 'late image must not create a texture');
    }
    assert.equal(staleReady, 0);
    assert.equal(scenario._textureCache.size, 0);
    assert.equal(scenario.sprite.texture, Texture.EMPTY);
    assert.equal(scenario.container.visible, false);
    assert.equal(scenario._textureLoading, true, 'late completion must not cancel the latest activation');
    await fetchReady(1); finishImage(images.length - 1);
    assert.equal(scenario._textureCache.size, 1);
    assert.equal(scenario.container.visible, true);
    assert.equal(scenario.active, true);
    assertBackground();
  }
  scenario.setCharacterBackend('live2d');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
"""


@pytest.mark.parametrize("case", ["release-and-reactivate", "late-fetch", "late-image"])
def test_scenario_backend_texture_lifecycle(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for renderer contracts")
    subprocess.run(
        [node, "-e", SCRIPT, case],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
