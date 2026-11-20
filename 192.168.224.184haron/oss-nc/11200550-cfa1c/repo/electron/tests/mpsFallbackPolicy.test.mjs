import assert from 'node:assert/strict'
import test from 'node:test'

import { defaultMpsFallbackEnvironment } from '../src/main/mpsFallbackPolicy.ts'

test('Apple Silicon local GPT-SoVITS defaults MPS fallback on', () => {
  assert.deepEqual(defaultMpsFallbackEnvironment('darwin', 'arm64', {
    TTS_BACKEND: 'gpt_sovits',
    TTS_DEVICE: 'auto',
  }), { PYTORCH_ENABLE_MPS_FALLBACK: '1' })
})

test('an explicit MPS fallback value is preserved', () => {
  assert.deepEqual(defaultMpsFallbackEnvironment('darwin', 'arm64', {
    TTS_BACKEND: 'gpt_sovits',
    TTS_DEVICE: 'mps',
    PYTORCH_ENABLE_MPS_FALLBACK: '0',
  }), {})
})

test('CPU and remote TTS paths do not receive the MPS fallback default', () => {
  assert.deepEqual(defaultMpsFallbackEnvironment('darwin', 'arm64', {
    TTS_BACKEND: 'gpt_sovits',
    TTS_DEVICE: 'cpu',
  }), {})
  assert.deepEqual(defaultMpsFallbackEnvironment('darwin', 'arm64', {
    TTS_BACKEND: 'openai_compatible',
    TTS_DEVICE: 'auto',
  }), {})
})

test('Intel macOS and non-macOS platforms do not receive the MPS fallback default', () => {
  assert.deepEqual(defaultMpsFallbackEnvironment('darwin', 'x64', {
    TTS_BACKEND: 'gpt_sovits',
    TTS_DEVICE: 'auto',
  }), {})
  assert.deepEqual(defaultMpsFallbackEnvironment('win32', 'arm64', {
    TTS_BACKEND: 'gpt_sovits',
    TTS_DEVICE: 'auto',
  }), {})
})
