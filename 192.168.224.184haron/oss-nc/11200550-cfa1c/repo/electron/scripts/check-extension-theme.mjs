// Author check: native resource owner and actual Chromium painting in a disposable profile.
import { app, BrowserWindow, session } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
const root = fileURLToPath(new URL('../../', import.meta.url))
const base = process.env.AMADEUS_CONTRACT_THEME_ROOT
app.setPath('userData', path.join(base, 'profile'))
app.whenReady().then(async () => {
  let window
  try {
    const record = JSON.parse(fs.readFileSync(process.env.AMADEUS_CONTRACT_THEME_RECORD, 'utf8'))
    const { ExtensionThemeResources } = await import('../dist/main/extensionThemes.js')
    const source = fs.readFileSync(path.join(root, 'electron/src/renderer/themeApplication.ts'), 'utf8')
    const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText
    const { createThemeApplication } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))
    session.defaultSession.setPermissionCheckHandler((_webContents, permission) => permission === 'local-fonts')
    session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => callback(permission === 'local-fonts'))
    window = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true, nodeIntegration: false } })
    await window.loadURL('data:text/html,<html><head></head><body><p>Theme contract</p></body></html>')
    const ui = expression => window.webContents.executeJavaScript(expression, true)
    const assets = path.join(root, 'electron/dist/renderer/assets')
    const css = fs.readdirSync(assets).filter(name => name.endsWith('.css')).map(name => fs.readFileSync(path.join(assets, name), 'utf8')).join('\n')
    await ui(`{ const style=document.createElement('style'); style.textContent=${JSON.stringify(css)}; document.head.appendChild(style); true }`)
    const resources = new ExtensionThemeResources(() => ui("(async()=>[...new Set((await queryLocalFonts()).map(f=>f.family))])()"))
    await resources.validate(record.manifest, record.package_path)
    await ui(`window.application=(${createThemeApplication.toString()})(document,URL); true`)
    let count = 0
    for (const definition of record.manifest.contributes.theme) {
      const themes = await resources.validate(record.manifest, record.package_path, definition.id)
      const theme = { ...themes[definition.id], id: record.manifest.id + '/' + definition.id, version: record.manifest.version, revision: 1 }
      await ui(`application.apply(${JSON.stringify({ selected: theme.id, base: theme.base, reason: '', theme })}); true`)
      const painted = await ui("({id:document.documentElement.dataset.themeExtension, background:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()})")
      if (painted.id !== theme.id || !painted.background) throw new Error('Theme did not paint')
      count++
    }
    await ui("application.apply({selected:'classic',base:'classic',reason:''}); true")
    if (await ui("Boolean(document.documentElement.dataset.themeExtension)")) throw new Error('Builtin restoration failed')
    fs.writeFileSync(path.join(base, 'theme-result.json'), JSON.stringify({ themes: count, nativeDecode: true, painted: true, builtinRestored: true, nativeDialogs: false, scope: 'Native hidden-window rendering; no interactive dialogs' }))
    window.destroy(); app.exit(0)
  } catch {
    window?.destroy(); app.exit(1)
  }
})
