// Node contract tests for render/web/electron_slice_host.js run the host IIFE
// in a vm context against a minimal fake DOM. They cover the optional
// freeControls layout: cover-crop projection, dragging, persistence, and
// right-click reset of the Work status button and task card.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '../render/web/electron_slice_host.js'), 'utf8');

const POSITIONS_KEY = 'amadeus.desktop-controls.positions.v1';

class FakeElement {
  constructor(className, rect) {
    this.className = className || '';
    this.rect = rect || { left: 0, top: 0, width: 0, height: 0 };
    this.style = {};
    this.children = [];
    this.parent = null;
    this.listeners = {};
    this.attributes = {};
  }

  get offsetParentRect() {
    for (let p = this.parent; p; p = p.parent) {
      if (p.style.position === 'absolute') return p.getBoundingClientRect();
    }
    return { left: 0, top: 0 };
  }

  getBoundingClientRect() {
    const offset = this.offsetParentRect;
    const left = this.style.left ? offset.left + Number.parseFloat(this.style.left) : this.rect.left;
    const top = this.style.top ? offset.top + Number.parseFloat(this.style.top) : this.rect.top;
    return {
      left, top, x: left, y: top,
      width: this.rect.width, height: this.rect.height,
      right: left + this.rect.width, bottom: top + this.rect.height,
    };
  }

  matches(selector) {
    if (selector.startsWith('.')) {
      return this.className.split(/\s+/).includes(selector.slice(1));
    }
    return false;
  }

  closest(selector) {
    for (let p = this; p; p = p.parent) {
      if (p.matches && p.matches(selector)) return p;
    }
    return null;
  }

  contains(element) {
    for (let p = element; p; p = p.parent) {
      if (p === this) return true;
    }
    return false;
  }

  setAttribute(name, value) { this.attributes[name] = String(value); }

  appendChild(child) { child.parent = this; this.children.push(child); return child; }

  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }

  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((item) => item !== fn);
  }

  dispatchEvent(type, event) {
    const payload = { type, target: this, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.propagationStopped = true; }, ...event };
    for (const fn of [...(this.listeners[type] || [])]) fn(payload);
    return payload;
  }
}

function createStorage(seed) {
  const map = new Map(Object.entries(seed || {}));
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
    map,
  };
}

function createHarness(options) {
  const opts = options || {};
  const innerWidth = opts.innerWidth || 2000;
  const innerHeight = opts.innerHeight || 1200;
  const storage = createStorage(opts.storage);

  const root = new FakeElement('crt-canvas-surface', opts.rootRect || { left: 600, top: 300, width: 700, height: 500 });
  root.style.position = 'absolute';
  const dot = new FakeElement('crt-canvas-surface-dot', { left: 1250, top: 310, width: 16, height: 16 });
  const status = new FakeElement('crt-canvas-surface-status', { left: 1150, top: 309, width: 80, height: 18 });
  const card = new FakeElement('crt-canvas-surface-card', { left: 630, top: 345, width: 500, height: 350 });
  card.style.position = 'absolute';
  const header = new FakeElement('crt-canvas-semantic-header', { left: 640, top: 350, width: 480, height: 30 });
  const cardBody = new FakeElement('crt-canvas-body', { left: 640, top: 390, width: 480, height: 280 });
  root.appendChild(dot);
  root.appendChild(status);
  root.appendChild(card);
  card.appendChild(header);
  card.appendChild(cardBody);
  const elements = { root, dot, status, card, header, cardBody };

  const registry = {
    '.crt-canvas-surface': [root],
    '.crt-canvas-surface-dot': [dot],
    '.crt-canvas-surface-status': [status],
    '.crt-canvas-surface-card': [card],
  };

  const documentListeners = {};
  const body = new FakeElement('body', { left: 0, top: 0, width: innerWidth, height: innerHeight });
  const document = {
    body,
    getElementById: () => null,
    querySelector: (selector) => (registry[selector] || [])[0] || null,
    querySelectorAll: (selector) => {
      const base = selector.replace(/:not\(\[hidden\]\)/g, '');
      return registry[base] || [];
    },
    addEventListener: (type, fn, capture) => {
      (documentListeners[type] = documentListeners[type] || []).push({ fn, capture: capture === true });
    },
    removeEventListener: () => {},
    dispatch: (type, event) => {
      const payload = { type, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.propagationStopped = true; }, ...event };
      for (const entry of [...(documentListeners[type] || [])]) entry.fn(payload);
      return payload;
    },
  };

  const windowListeners = {};
  const surfaceCalls = [];
  const surface = {
    layout: (rect) => surfaceCalls.push(rect),
    setPayload() {}, toggle() {}, setPresentation() {}, setAttention() {},
  };
  const window = {
    location: {
      search: opts.search || '',
      origin: 'http://127.0.0.1:17778',
      reload() { window.reloaded = true; },
    },
    innerWidth,
    innerHeight,
    createCrtCanvasSurface: () => surface,
    addEventListener: (type, fn, capture) => {
      (windowListeners[type] = windowListeners[type] || []).push({ fn, capture: capture === true });
    },
    removeEventListener: (type, fn, capture) => {
      windowListeners[type] = (windowListeners[type] || []).filter((entry) => entry.fn !== fn);
    },
    dispatch: (type, event) => {
      const payload = { type, ...event };
      for (const entry of [...(windowListeners[type] || [])]) entry.fn(payload);
      return payload;
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
    getComputedStyle: () => ({ display: '', visibility: '', pointerEvents: '' }),
    localStorage: storage,
    amadeus: { setElectronSliceShape: async () => true },
  };
  // Keep removeEventListener semantics on (fn, capture): the host re-registers
  // the same functions with capture=true, so entries match on fn alone.
  window.removeEventListener = (type, fn) => {
    windowListeners[type] = (windowListeners[type] || []).filter((entry) => entry.fn !== fn);
  };

  const context = {
    window,
    document,
    localStorage: storage,
    console,
    fetch: async () => ({
      ok: true,
      json: async () => opts.bridgeInfo || {
        bridgePort: 17797,
        bridgeToken: 'token',
        sliceBounds: { x: 0.3, y: 0.2, width: 0.4, height: 0.5 },
        canvasBounds: { x: 0.35, y: 0.22, width: 0.3, height: 0.45 },
        keyboardInputToggleBounds: { x: 0.7, y: 0.8, width: 0.12, height: 0.03 },
        keyboardComposerBounds: { x: 0.35, y: 0.75, width: 0.3, height: 0.12 },
        wallpaperImageSize: [1672, 941],
      },
    }),
    EventSource: class {
      constructor() { this.onopen = null; this.onmessage = null; this.onerror = null; }
      close() {}
    },
    MutationObserver: class { constructor() {} observe() {} disconnect() {} },
    URLSearchParams,
    JSON,
    Promise,
  };
  context.window.window = window;
  vm.createContext(context);
  vm.runInContext(source, context);
  return { context, window, document, elements, storage, surfaceCalls };
}

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 80));
}

test('default mode keeps slice-relative projection and ignores saved positions', async () => {
  const harness = createHarness({
    search: '?windowsComposer=1&bridgePort=17797',
    storage: { [POSITIONS_KEY]: JSON.stringify({ status: { x: 0.01, y: 0.01 } }) },
  });
  await settle();
  const { surfaceCalls, elements } = harness;
  // canvasBounds {x:0.35,y:0.22,w:0.3,h:0.45} inside sliceBounds {x:0.3,y:0.2,w:0.4,h:0.5}
  // on a 2000x1200 slice window maps to the authored sub-rect.
  const canvas = surfaceCalls[surfaceCalls.length - 1];
  assert.ok(Math.abs(canvas.x - 250) < 1e-6, JSON.stringify(canvas));
  assert.ok(Math.abs(canvas.y - 48) < 1e-6, JSON.stringify(canvas));
  assert.ok(Math.abs(canvas.width - 1500) < 1e-6, JSON.stringify(canvas));
  assert.ok(Math.abs(canvas.height - 1080) < 1e-6, JSON.stringify(canvas));
  assert.equal(elements.status.style.left, undefined);
  assert.equal(elements.root.style.overflow, undefined);
});

test('freeControls projects authored bounds through the wallpaper cover crop', async () => {
  const harness = createHarness({ search: '?windowsComposer=1&freeControls=1&bridgePort=17797' });
  await settle();
  const { surfaceCalls, elements, window } = harness;
  // 1672x941 art cover-fills a 2000x1200 window: scale = max(2000/1672, 1200/941).
  const scale = Math.max(2000 / 1672, 1200 / 941);
  const width = 1672 * scale;
  const height = 941 * scale;
  const offsetX = (2000 - width) / 2;
  const offsetY = (1200 - height) / 2;
  const b = { x: 0.35, y: 0.22, width: 0.3, height: 0.45 };
  const canvas = surfaceCalls[surfaceCalls.length - 1];
  assert.ok(Math.abs(canvas.x - (offsetX + b.x * width)) < 1e-9);
  assert.ok(Math.abs(canvas.y - (offsetY + b.y * height)) < 1e-9);
  assert.ok(Math.abs(canvas.width - b.width * width) < 1e-9);
  assert.ok(Math.abs(canvas.height - b.height * height) < 1e-9);
  assert.equal(elements.root.style.overflow, 'visible');
  void window;
});

test('dragging the status button persists a normalized position and swallows the click', async () => {
  const harness = createHarness({ search: '?windowsComposer=1&freeControls=1&bridgePort=17797' });
  await settle();
  const { elements, window, document, storage } = harness;
  elements.status.dispatchEvent('pointerdown', { button: 0, clientX: 1160, clientY: 315 });
  // Below the drag threshold nothing is pinned and no position is stored.
  window.dispatch('pointermove', { clientX: 1162, clientY: 316 });
  assert.equal(elements.status.style.left, undefined);
  window.dispatch('pointermove', { clientX: 1240, clientY: 360 });
  const rootLeft = elements.root.getBoundingClientRect().left;
  const rootTop = elements.root.getBoundingClientRect().top;
  assert.equal(elements.status.style.left, `${Math.round(1230 - rootLeft)}px`);
  assert.equal(elements.status.style.top, `${Math.round(354 - rootTop)}px`);
  assert.equal(elements.status.style.right, 'auto');
  window.dispatch('pointerup', { clientX: 1240, clientY: 360 });

  const saved = JSON.parse(storage.getItem(POSITIONS_KEY));
  assert.ok(saved.status);
  assert.ok(Math.abs(saved.status.x - 1230 / 2000) < 1e-9, JSON.stringify(saved));
  assert.ok(Math.abs(saved.status.y - 354 / 1200) < 1e-9, JSON.stringify(saved));

  const click = document.dispatch('click', { target: elements.status });
  assert.equal(click.defaultPrevented, true);
  assert.equal(click.propagationStopped, true);
});

test('a click without dragging still toggles normally', async () => {
  const harness = createHarness({ search: '?windowsComposer=1&freeControls=1&bridgePort=17797' });
  await settle();
  const { elements, window, document, storage } = harness;
  elements.status.dispatchEvent('pointerdown', { button: 0, clientX: 1160, clientY: 315 });
  window.dispatch('pointerup', { clientX: 1160, clientY: 315 });
  assert.equal(elements.status.style.left, undefined);
  assert.equal(storage.getItem(POSITIONS_KEY), null);
  const click = document.dispatch('click', { target: elements.status });
  assert.equal(click.defaultPrevented, undefined);
});

test('right-click resets the control to its authored position', async () => {
  const harness = createHarness({
    search: '?windowsComposer=1&freeControls=1&bridgePort=17797',
    storage: { [POSITIONS_KEY]: JSON.stringify({ status: { x: 0.5, y: 0.5 } }) },
  });
  await settle();
  const { elements, storage } = harness;
  // The saved position was applied relative to the surface root.
  const rootLeft = elements.root.getBoundingClientRect().left;
  const rootTop = elements.root.getBoundingClientRect().top;
  assert.equal(elements.status.style.left, `${Math.round(0.5 * 2000 - rootLeft)}px`);
  assert.equal(elements.status.style.top, `${Math.round(0.5 * 1200 - rootTop)}px`);

  const menu = elements.status.dispatchEvent('contextmenu', { button: 2 });
  assert.equal(menu.defaultPrevented, true);
  assert.equal(elements.status.style.left, '');
  assert.equal(elements.status.style.top, '');
  assert.equal(elements.status.style.right, '');
  assert.deepEqual(JSON.parse(storage.getItem(POSITIONS_KEY)), {});
});

test('the task card drags only from its semantic header', async () => {
  const harness = createHarness({ search: '?windowsComposer=1&freeControls=1&bridgePort=17797' });
  await settle();
  const { elements, window, storage } = harness;
  // A press inside the card body never starts a drag.
  elements.card.dispatchEvent('pointerdown', { button: 0, clientX: 700, clientY: 450, target: elements.cardBody });
  window.dispatch('pointermove', { clientX: 800, clientY: 550 });
  window.dispatch('pointerup', { clientX: 800, clientY: 550 });
  assert.equal(elements.card.style.left, undefined);
  assert.equal(storage.getItem(POSITIONS_KEY), null);

  elements.card.dispatchEvent('pointerdown', { button: 0, clientX: 700, clientY: 360, target: elements.header });
  window.dispatch('pointermove', { clientX: 800, clientY: 460 });
  const rootLeft = elements.root.getBoundingClientRect().left;
  const rootTop = elements.root.getBoundingClientRect().top;
  assert.equal(elements.card.style.left, `${Math.round(730 - rootLeft)}px`);
  assert.equal(elements.card.style.top, `${Math.round(445 - rootTop)}px`);
  window.dispatch('pointerup', { clientX: 800, clientY: 460 });
  const saved = JSON.parse(storage.getItem(POSITIONS_KEY));
  assert.ok(Math.abs(saved.card.x - 730 / 2000) < 1e-9, JSON.stringify(saved));
  assert.ok(Math.abs(saved.card.y - 445 / 1200) < 1e-9, JSON.stringify(saved));
});
