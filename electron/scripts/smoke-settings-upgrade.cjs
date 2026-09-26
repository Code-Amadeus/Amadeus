// Opt-in release check: real Electron settings from an earlier Git version,
// loaded by the current build, without opening or changing a user's profile.
// electron scripts/smoke-settings-upgrade.cjs v0.15.0-alpha.0
const { app, safeStorage } = require('electron')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const { pathToFileURL } = require('node:url')
const ts = require('typescript')

const root = path.resolve(__dirname, '../..')
const previousRef = process.argv[2] || 'v0.15.0-alpha.0'
const outputRoot = path.join(root, 'electron/build/settings-upgrade')
fs.mkdirSync(outputRoot, { recursive: true })
const output = fs.mkdtempSync(path.join(outputRoot, 'run-'))
app.setPath('userData', path.join(output, 'profile'))

app.whenReady().then(async () => {
  assert.ok(safeStorage.isEncryptionAvailable(), 'Native credential encryption must be available')
  const oldSource = execFileSync('git', [
    'show', previousRef + ':electron/src/main/desktopSettings.ts',
  ], { cwd: root, encoding: 'utf8', windowsHide: true })
  const previousModule = path.join(output, 'previous-settings.mjs')
  fs.writeFileSync(previousModule, ts.transpileModule(oldSource, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  }).outputText)
  const { DesktopSettingsStore: PreviousStore } = await import(pathToFileURL(previousModule).href)
  const { DesktopSettingsStore: CurrentStore } = await import(
    pathToFileURL(path.join(root, 'electron/dist/main/desktopSettings.js')).href
  )
  const file = path.join(output, 'settings.json')
  const dotenv = path.join(output, '.env')
  // The previous release configured the cooperative provider through .env;
  // it was not a supported DesktopSettingsStore value.
  const legacyDotenv = 'COOPERATIVE_CHAT_PROVIDER=openclaw\nTTS_OUTPUT_LANGUAGE=英文\n'
  fs.writeFileSync(dotenv, legacyDotenv)
  const legacyValues = {
    LLM_PROVIDER: 'deepseek',
    DEEPSEEK_MODEL_NAME: 'deepseek-v4-flash',
    CODEX_PROVIDER_TRANSPORT: 'direct',
    TTS_DEVICE: 'cpu',
  }
  const secret = 'test-release-upgrade-credential'
  new PreviousStore(file, dotenv).update({}, {
    values: legacyValues, secrets: { DEEPSEEK_API_KEY: secret },
  })
  const previousBytes = fs.readFileSync(file)
  const current = new CurrentStore(file, dotenv)
  const migrated = current.snapshot({})
  assert.deepEqual(migrated.values, legacyValues)
  assert.deepEqual(fs.readFileSync(file), previousBytes, 'Reading must not rewrite the old profile')
  assert.equal(current.backendEnvironment({}).DEEPSEEK_API_KEY, secret)
  assert.equal(migrated.sources.COOPERATIVE_CHAT_PROVIDER, 'dotenv')
  assert.equal(fs.readFileSync(dotenv, 'utf8'), legacyDotenv)
  assert.equal(current.backendEnvironment({}).DIRECT_CODEX_PROVIDER_ENABLED, 'true')

  current.update({}, { values: {
    WORK_CODING_PROVIDER: 'codex', WORK_EXECUTION_PROVIDER: 'pi',
    AMADEUS_WINDOWS_STARTUP_MODE: 'wallpaper',
  } })
  const reopened = new CurrentStore(file, dotenv)
  const values = reopened.snapshot({}).values
  for (const [key, value] of Object.entries(legacyValues)) assert.equal(values[key], value)
  assert.equal(values.WORK_CODING_PROVIDER, 'codex')
  assert.equal(values.WORK_EXECUTION_PROVIDER, 'pi')
  assert.equal(values.AMADEUS_WINDOWS_STARTUP_MODE, 'wallpaper')
  assert.equal(reopened.backendEnvironment({}).DEEPSEEK_API_KEY, secret)
  const environment = reopened.backendEnvironment({})
  // Preserve environment output locally for checking Python migration aliases.
  delete environment.DEEPSEEK_API_KEY
  const report = { status: 'passed', previousRef, legacyValuesPreserved: true,
    encryptedCredentialPreserved: true, independentRolesPersisted: true,
    startupPreferencePersisted: true, environment }
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  console.log('PASS previous-release settings and native encrypted credential survive upgrade; new roles and startup preference survive reopening')
  app.quit()
}).catch(error => { console.error(error); app.exit(1) })
