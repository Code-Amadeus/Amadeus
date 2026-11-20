import type { DesktopThemeState } from '../shared/extensionTheme.js'

/** One style and one object URL per renderer, released at every actual switch. */
export function createThemeApplication(document: Document, urls = URL) {
  let style: HTMLStyleElement | undefined, imageUrl: string | undefined, identity = ''
  const clear = () => {
    style?.remove(); style = undefined
    if (imageUrl) urls.revokeObjectURL(imageUrl)
    imageUrl = undefined
    delete document.documentElement.dataset.themeExtension
  }
  return {
    apply(state: DesktopThemeState) {
      const nextIdentity = JSON.stringify([state.selected, state.base, state.reason, state.theme?.id, state.theme?.revision, state.theme?.version])
      if (nextIdentity === identity) return
      identity = ''
      clear()
      const root = document.documentElement
      root.dataset.theme = state.base
      root.style.colorScheme = state.base === 'wallpaper-slice' ? 'dark' : 'light'
      if (state.theme) {
        root.dataset.themeExtension = state.theme.id
        style = document.createElement('style')
        style.dataset.amadeusExtensionTheme = 'true'
        const selector = `:root[data-theme-extension="${state.theme.id}"]`
        let css = `${selector} { ${Object.entries(state.theme.tokens).map(([key, value]) => `${key}: ${value};`).join(' ')} background:var(--bg); }
          ${selector} body, ${selector} #root, ${selector} .desktop-main-content { background:transparent; }`
        if (state.theme.image && state.theme.background) {
          const bytes = Uint8Array.from(atob(state.theme.image.base64), char => char.charCodeAt(0))
          imageUrl = urls.createObjectURL(new Blob([bytes], { type: state.theme.image.mime }))
          const background = state.theme.background
          css += `${selector} { background-image: linear-gradient(var(--bg),var(--bg)),linear-gradient(rgba(0,0,0,${background.dim}),rgba(0,0,0,${background.dim})),url("${imageUrl}"); background-position:center; background-repeat:no-repeat; background-size:${background.fit}; background-attachment:fixed; }`
        }
        style.textContent = css
        document.head.appendChild(style)
      }
      identity = nextIdentity
    },
    dispose() { identity = ''; clear() },
  }
}
