import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/renderer/components/desktopRuntimeSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const exports = {}
new Function('exports', compiled)(exports)

test('runtime controls project to durable backend environment settings', () => {
  assert.deepEqual(exports.desktopValuesForRuntimeSettings({
    llm_provider: 'openai',
    vision_enabled: true,
    vision_mode: 'watching',
    presentation_locale: 'zh-CN',
    wallpaper_caption_mode: 'bilingual',
    chat_translation_subtitles_enabled: true,
    tts_mode: 'cuda_graph',
    tts_output_language: 'en',
  }), {
    LLM_PROVIDER: 'openai',
    AMADEUS_VISION_ENABLED: true,
    AMADEUS_VISION_MODE: 'watching',
    AMADEUS_PRESENTATION_LOCALE: 'zh-CN',
    AMADEUS_WALLPAPER_CAPTION_MODE: 'bilingual',
    AMADEUS_CHAT_TRANSLATION_SUBTITLES_ENABLED: true,
    ENABLE_CUDA_GRAPH: '1',
    EXP_TTS_MAX_CONCURRENCY: '1',
    TTS_OUTPUT_LANGUAGE: '英文',
  })
})

test('saved desktop runtime values can render while the backend is offline', () => {
  const values = {
    AMADEUS_VISION_ENABLED: 'true',
    AMADEUS_VISION_SCOPE: 'selected_window',
    ENABLE_CUDA_GRAPH: '1',
    EXP_TTS_MAX_CONCURRENCY: '1',
    TTS_OUTPUT_LANGUAGE: '英文',
  }
  assert.equal(exports.runtimeSettingFromDesktopValues('vision_enabled', values), true)
  assert.equal(exports.runtimeSettingFromDesktopValues('vision_scope', values), 'selected_window')
  assert.equal(exports.runtimeSettingFromDesktopValues('tts_mode', values), 'cuda_graph')
  assert.equal(exports.runtimeSettingFromDesktopValues('tts_output_language', values), 'en')
})

test('standard synthesis defaults to one task while explicit parallel mode keeps two', () => {
  assert.deepEqual(exports.desktopValuesForRuntimeSettings({ tts_mode: 'parallel' }), {
    ENABLE_CUDA_GRAPH: '0', EXP_TTS_MAX_CONCURRENCY: '1',
  })
  assert.deepEqual(exports.desktopValuesForRuntimeSettings({ tts_mode: 'parallel2' }), {
    ENABLE_CUDA_GRAPH: '0', EXP_TTS_MAX_CONCURRENCY: '2',
  })
  assert.equal(exports.runtimeSettingFromDesktopValues('tts_mode', {
    ENABLE_CUDA_GRAPH: '0', EXP_TTS_MAX_CONCURRENCY: '1',
  }), 'parallel')
  assert.equal(exports.runtimeSettingFromDesktopValues('tts_mode', {
    ENABLE_CUDA_GRAPH: '0', EXP_TTS_MAX_CONCURRENCY: '2',
  }), 'parallel2')
})
