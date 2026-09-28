/** Keep an explicit selection; otherwise use the Host-configured default.
 * An unavailable default requires selection, never an implicit provider switch.
 */
export function preserveOrChooseProvider(
  current: string,
  providers: string[],
  defaultProvider: string,
): string {
  if (providers.includes(current)) return current
  return providers.includes(defaultProvider) ? defaultProvider : ''
}
