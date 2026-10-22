import { useEffect, useState } from 'react'

export function extensionInitial(name: string): string {
  const first = new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(name.trim())[Symbol.iterator]().next().value
  return first?.segment.toLocaleUpperCase() || '?'
}

/** Decorative package identity. Remount for a new package digest so old image replies cannot win. */
export default function ExtensionIcon({ id, name, icon }: { id: string; name: string; icon?: string }) {
  const [source, setSource] = useState<string | null>(null)
  useEffect(() => {
    let current = true
    if (icon && window.amadeus) {
      window.amadeus.getExtensionIcon(id).then(image => { if (current) setSource(image) }, () => {})
    }
    return () => { current = false }
  }, [id, icon])
  return <span className="extension-icon" aria-hidden="true">
    {source ? <img src={source} alt="" onError={() => setSource(null)} /> : extensionInitial(name)}
  </span>
}
