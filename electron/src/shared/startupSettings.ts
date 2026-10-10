/** Public, non-secret startup inputs. Stored overrides are not effective values. */
export interface StartupSnapshot {
  values?: Record<string, string>
  startupValues?: Record<string, string>
  sources?: Record<string, 'environment' | 'user' | 'dotenv' | 'default'>
  pendingRevisions?: Record<string, number>
}

export function startupValue(
  key: string,
  snapshot: StartupSnapshot | null | undefined,
  fallback?: string | number | boolean,
  effective?: string | number | boolean,
): string | number | boolean | undefined {
  const source = snapshot?.sources?.[key]
  const known = snapshot?.startupValues?.[key]
  if (known !== undefined) return known
  if (source === 'user' || !source) return snapshot?.values?.[key] ?? effective ?? fallback
  // A running backend predates a pending clear. Its old value cannot describe
  // the next launch; dotenv interpolation is owned by Python, so stay unknown.
  const current = snapshot?.pendingRevisions?.[key] ? undefined : effective
  if (source === 'environment' || source === 'dotenv') return current
  // With no startup override, the declaration/owner computes the next launch.
  // A running value can belong to an older inherited profile or a live edit.
  return fallback
}

export function startupValues(snapshot: StartupSnapshot | null | undefined): Record<string, string> {
  return Object.fromEntries([...new Set([
    ...Object.keys(snapshot?.values || {}), ...Object.keys(snapshot?.startupValues || {}),
  ])].flatMap(key => {
    const value = startupValue(key, snapshot)
    return value === undefined ? [] : [[key, String(value)]]
  }))
}

export function projectStartupFields<T extends { key: string; type: string; value?: string | boolean; options?: unknown; true_values?: string[] }>(
  fields: T[], effective: T[] | undefined, snapshot: StartupSnapshot | null | undefined,
): T[] {
  return fields.map(field => {
    if (field.type === 'secret') return field
    const running = effective?.find(item => item.key === field.key)
    const raw = startupValue(field.key, snapshot, field.value, running?.value)
    const value = raw === undefined ? undefined : field.type === 'boolean'
      ? raw === true || (field.true_values ?? ['true', '1', 'yes']).includes(String(raw).trim().toLowerCase()) : String(raw)
    return { ...field, value, ...(running?.options ? { options: running.options } : {}) }
  })
}
