import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import ts from 'typescript'

const root = path.resolve(import.meta.dirname, '..')
const sourcePath = path.join(root, 'src', 'renderer', 'components', 'chatMessageState.ts')
const source = fs.readFileSync(sourcePath, 'utf8')
const js = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText

const sandbox = { exports: {}, module: { exports: {} } }
Object.defineProperty(sandbox.module, 'exports', {
  get: () => sandbox.exports,
  set: value => { sandbox.exports = value },
})
vm.runInNewContext(js, sandbox, { filename: sourcePath })
const { patchInterruptedMessage } = sandbox.exports

function assertEqual(actual, expected, label) {
  if (actual !== expected) {
    throw new Error(`${label}\nexpected: ${expected}\nactual:   ${actual}`)
  }
}

let messages = []
const turnId = 'turn-a'

// User sends a prompt.
messages = [...messages, { role: 'user', text: '介绍一下 Paxos 理论。' }]

// LLM streams faster than physical TTS playback, so the chat bubble shows more
// than the user actually heard.
messages = [
  ...messages,
  {
    role: 'assistant',
    turnId,
    text: 'あー、やっと日本語で話したわね。\n\nPaxosは分散システムで合意形成を行うためのプロトコルよ。続きの説明も全部出ている。',
    streaming: true,
  },
]

// Barge-in interrupts after only the first sentence was physically played.
messages = patchInterruptedMessage(messages, {
  turnId,
  text: 'あー、やっと日本語で話したわね。 [interrupted by user]',
  marker: '[interrupted by user]',
})

assertEqual(messages.length, 2, 'interrupt should patch, not append')
assertEqual(messages[1].streaming, false, 'assistant bubble should stop streaming')
assertEqual(
  messages[1].text,
  'あー、やっと日本語で話したわね。 [interrupted by user]',
  'assistant bubble should roll back to server interrupted text',
)

// A later stale chat.complete from the aborted turn would be ignored by
// ChatPage because the turn id is marked interrupted. This script only verifies
// the deterministic patch behavior that used to be fragile in the component.
console.log(JSON.stringify(messages, null, 2))
console.log('chat interrupt repro ok')
