import fs from 'node:fs'
import { createRequire } from 'node:module'
import ts from 'typescript'

export function createSourceRequire(url, mocks = {}, cache = new Map()) {
  const nativeRequire = createRequire(url)
  return name => {
    if (Object.hasOwn(mocks, name)) return mocks[name]
    if (name.startsWith('.')) {
      const target = new URL(name, url)
      const stem = target.href.replace(/\.js$/, '')
      const candidate = [target, new URL(`${stem}.ts`), new URL(`${stem}.tsx`)].find(item => fs.existsSync(item))
      if (candidate && /\.tsx?$/.test(candidate.pathname)) return loadTypeScript(candidate, mocks, cache)
    }
    return nativeRequire(name)
  }
}

// Load source modules with their real relative dependencies and explicit mocks.
// This keeps unit tests independent of dist/ and the Electron application process.
export function loadTypeScript(url, mocks = {}, cache = new Map()) {
  if (cache.has(url.href)) return cache.get(url.href)
  const source = fs.readFileSync(url, 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText
  const module = { exports: {} }
  cache.set(url.href, module.exports)
  const require = createSourceRequire(url, mocks, cache)
  new Function('require', 'module', 'exports', code)(require, module, module.exports)
  return module.exports
}
