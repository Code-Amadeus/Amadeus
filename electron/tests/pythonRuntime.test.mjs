import assert from 'node:assert/strict'
import path from 'node:path'
import test from 'node:test'
import { resolvePythonCommand } from '../src/main/pythonRuntime.ts'

test('uses an explicitly configured interpreter when it exists', () => {
  assert.equal(resolvePythonCommand({
    projectRoot: 'D:/repo',
    platform: 'win32',
    environment: { AMADEUS_PYTHON: 'D:/Python/python.exe' },
    exists: candidate => candidate === 'D:/Python/python.exe',
  }), 'D:/Python/python.exe')
})

test('uses the repository virtual environment before machine installs', () => {
  const expected = path.join('D:/repo', '.venv', 'Scripts', 'python.exe')
  assert.equal(resolvePythonCommand({
    projectRoot: 'D:/repo',
    platform: 'win32',
    environment: { LOCALAPPDATA: 'C:/Users/test/AppData/Local' },
    exists: candidate => candidate === expected,
  }), expected)
})

test('finds a Conda environment named after the project', () => {
  const registry = path.join('C:/Users/test', '.conda', 'environments.txt')
  const expected = path.join('D:/CondaEnvs/Amadeus', 'python.exe')
  assert.equal(resolvePythonCommand({
    projectRoot: 'D:/Projects/Amadeus',
    platform: 'win32',
    environment: { USERPROFILE: 'C:/Users/test', LOCALAPPDATA: 'C:/Users/test/AppData/Local' },
    exists: candidate => candidate === registry || candidate === expected,
    readText: candidate => candidate === registry
      ? 'D:/Anaconda3\r\nD:/CondaEnvs/Amadeus\r\n'
      : '',
  }), expected)
})

test('falls back to python on Windows without trusting Store aliases', () => {
  assert.equal(resolvePythonCommand({
    projectRoot: 'D:/repo',
    platform: 'win32',
    environment: { LOCALAPPDATA: 'C:/Users/test/AppData/Local' },
    exists: () => false,
  }), 'python')
})

test('falls back to python3 on Unix', () => {
  assert.equal(resolvePythonCommand({
    projectRoot: '/repo',
    platform: 'linux',
    environment: {},
    exists: () => false,
  }), 'python3')
})
