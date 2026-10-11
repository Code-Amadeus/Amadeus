import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
import {loadTypeScript} from './helpers/loadTypeScript.mjs'
const root = fileURLToPath(new URL('../../', import.meta.url))
const base = path.resolve(process.env.AMADEUS_TEST_DSH_STATE || path.join(root, 'build/deepseek-acp-live'))
const relative = path.relative(path.join(root, 'build'), base)
assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative))
fs.mkdirSync(base, {recursive:true})
const mocks = {electron:{safeStorage:{isEncryptionAvailable:()=>true, encryptString:v=>Buffer.from(v), decryptString:v=>v.toString()}}}
const {DesktopSettingsStore} = loadTypeScript(new URL('../src/main/desktopSettings.ts',import.meta.url),mocks)
const {ExtensionInstaller,ExtensionPackageHelper} = loadTypeScript(new URL('../src/main/extensions.ts',import.meta.url),mocks)
const env = {...process.env,PIP_NO_INDEX:'1',PIP_FIND_LINKS:path.join(root,'build/extension-examples')}
const python = process.env.AMADEUS_TEST_PYTHON
assert.ok(python)
const helper = new ExtensionPackageHelper(root,()=>python,env)
const store = new DesktopSettingsStore(path.join(base,'settings.json'),path.join(base,'.env'),process.platform,helper.validateRecord)
const installer = new ExtensionInstaller(path.join(base,'extensions'),root,store,helper,()=>python,env,undefined,async()=>true)
const phase = process.argv[2] || 'prepare'
if (phase === 'prepare') {
  for (const [wheel,id] of [['amadeus_example_handoff_skill-0.1.0-py3-none-any.whl','com.example.handoff-skill'],['amadeus_example_csv_audit_skill-0.1.0-py3-none-any.whl','com.example.csv-audit-skill'],['amadeus_example_notes_mcp-0.1.0-py3-none-any.whl','com.example.notes-mcp']]) {
    await installer.install(path.join(root,'build/extension-examples',wheel))
    await installer.update({id,enabled:true,providers:['deepseek_live']})
  }
} else if (phase === 'update') {
  await installer.install(path.join(root,'build/extension-examples','amadeus_example_handoff_skill-0.2.0-py3-none-any.whl'))
  await installer.cleanupStoppedBackend()
} else if (phase === 'remove') {
  for (const record of store.extensionRecords()) await installer.remove(record.id)
  await installer.cleanupStoppedBackend()
} else throw new Error('Unknown phase')
fs.writeFileSync(path.join(base,phase+'-snapshot.json'),await installer.launchSnapshot())
console.log(JSON.stringify({phase,ids:store.extensionRecords().map(r=>r.id)}))
