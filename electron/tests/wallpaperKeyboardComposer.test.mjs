import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
const composerSource = fs.readFileSync(
  path.join(root, 'render', 'web', 'electron_keyboard_composer.js'),
  'utf8',
)

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName
    this.hidden = false
    this.disabled = false
    this.value = ''
    this.style = { setProperty() {} }
    this.listeners = new Map()
    this.queries = new Map()
  }

  set innerHTML(value) {
    this._innerHTML = value
    if (value.includes('wallpaper-keyboard-composer-input')) {
      this.queries.set('.wallpaper-keyboard-composer-input', new FakeElement('textarea'))
      this.queries.set('.wallpaper-keyboard-composer-form', new FakeElement('form'))
      this.queries.set('.wallpaper-keyboard-composer-send', new FakeElement('button'))
      this.queries.set('.wallpaper-keyboard-composer-close', new FakeElement('button'))
      this.queries.set('.wallpaper-keyboard-composer-status', new FakeElement('span'))
    }
    if (value.includes('wallpaper-keyboard-composer-toggle')) {
      this.queries.set('button', [new FakeElement('button'), new FakeElement('button')])
    }
  }

  get innerHTML() {
    return this._innerHTML || ''
  }

  querySelector(selector) {
    return this.queries.get(selector) || null
  }

  querySelectorAll(selector) {
    return this.queries.get(selector) || []
  }

  setAttribute() {}

  focus() {}

  addEventListener(type, listener) {
    this.listeners.set(type, listener)
  }

  dispatchKey(event) {
    const listener = this.listeners.get('keydown')
    if (!listener) throw new Error('Composer did not register a keydown handler')
    listener(event)
    // Model the textarea's native Shift+Enter behavior only when the real
    // handler leaves the event alone.
    if (!event.defaultPrevented && event.key === 'Enter' && event.shiftKey) {
      this.value += '\n'
    }
  }
}

function keyEvent({ key, shiftKey = false, isComposing = false, keyCode = 0 }) {
  return {
    key,
    shiftKey,
    isComposing,
    keyCode,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true
    },
  }
}

function createComposerFixture() {
  const roots = []
  const fetchCalls = []
  const document = {
    body: {
      append(...items) {
        roots.push(...items)
      },
    },
    createElement(tagName) {
      return new FakeElement(tagName)
    },
  }
  const window = {
    setTimeout() {
      return 0
    },
    getComputedStyle() {
      return { lineHeight: '16', paddingTop: '0', paddingBottom: '0' }
    },
  }
  const context = {
    window,
    document,
    Array,
    String,
    Number,
    fetch: async (url, options) => {
      fetchCalls.push({ url, options })
      return {
        ok: true,
        json: async () => ({ ok: true, status: 'ok' }),
      }
    },
    console: { warn() {} },
  }
  vm.runInNewContext(composerSource, context, { filename: 'electron_keyboard_composer.js' })
  const composer = window.createWallpaperKeyboardComposer()
  const composerRoot = roots.find((rootElement) => rootElement.queries.has('.wallpaper-keyboard-composer-input'))
  return {
    composer,
    input: composerRoot.querySelector('.wallpaper-keyboard-composer-input'),
    fetchCalls,
  }
}

async function flushAsyncWork() {
  await new Promise((resolve) => setImmediate(resolve))
}

test('IME candidate confirmation does not send or clear the wallpaper composer draft', async () => {
  const { composer, input, fetchCalls } = createComposerFixture()
  composer.configure('17797', 'bridge-token')
  input.value = 'nihao'

  const event = keyEvent({ key: 'Enter', isComposing: true, keyCode: 229 })
  input.dispatchKey(event)
  await flushAsyncWork()

  assert.equal(event.defaultPrevented, false)
  assert.equal(input.value, 'nihao')
  assert.deepEqual(fetchCalls, [])
})

test('ordinary Enter sends the wallpaper composer draft', async () => {
  const { composer, input, fetchCalls } = createComposerFixture()
  composer.configure('17797', 'bridge-token')
  input.value = 'send this'

  const event = keyEvent({ key: 'Enter' })
  input.dispatchKey(event)
  await flushAsyncWork()

  assert.equal(event.defaultPrevented, true)
  assert.equal(input.value, '')
  assert.equal(fetchCalls.length, 1)
  assert.equal(fetchCalls[0].url, 'http://127.0.0.1:17797/wallpaper/chat-action')
  assert.equal(fetchCalls[0].options.body, JSON.stringify({ text: 'send this' }))
})

test('Shift+Enter keeps the wallpaper composer draft and inserts a newline', () => {
  const { input, fetchCalls } = createComposerFixture()
  input.value = 'first line'

  const event = keyEvent({ key: 'Enter', shiftKey: true })
  input.dispatchKey(event)

  assert.equal(event.defaultPrevented, false)
  assert.equal(input.value, 'first line\n')
  assert.deepEqual(fetchCalls, [])
})
