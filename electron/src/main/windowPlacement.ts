export function wantsFloatingCompanion(
  args = process.argv,
  environment = process.env,
): boolean {
  if (args.includes('--no-floating-companion')) return false
  return args.includes('--floating-companion') || environment.AMADEUS_FLOATING_COMPANION === '1'
}

export type DisplayBounds = {
  x: number
  y: number
  width: number
  height: number
}

export type DisplayLike = {
  id: number
  bounds: DisplayBounds
  workArea?: DisplayBounds
}

export type MainWindowPlacement = {
  bounds: DisplayBounds
  displayId: number
  fullscreen: boolean
  secondary: boolean
}

export type FloatingCompanionPlacement = {
  bounds: DisplayBounds
  dedicatedDisplay: boolean
  displayId: number
}

export function resolveFloatingCompanionBounds(workArea: DisplayBounds): DisplayBounds {
  const margin = 24
  const availableWidth = Math.max(1, workArea.width - margin * 2)
  const availableHeight = Math.max(1, workArea.height - margin * 2)
  const width = Math.min(480, Math.max(360, Math.round(workArea.width * 0.20)), availableWidth)
  const height = Math.min(820, Math.max(560, Math.round(workArea.height * 0.68)), availableHeight)
  return {
    x: Math.max(workArea.x, workArea.x + workArea.width - width - margin),
    y: Math.max(workArea.y, workArea.y + workArea.height - height - margin),
    width,
    height,
  }
}

function requestedDisplay(
  displays: readonly DisplayLike[],
  primary: DisplayLike,
  preference: string,
  fallback: 'primary' | 'secondary',
): DisplayLike {
  const normalized = String(preference || fallback).trim().toLowerCase()
  if (normalized === 'primary') return primary

  const requestedId = Number(normalized)
  if (Number.isInteger(requestedId)) {
    const exact = displays.find(display => display.id === requestedId)
    if (exact) return exact
  }

  return displays.find(display => display.id !== primary.id) || primary
}

function centeredWindowBounds(display: DisplayLike): DisplayBounds {
  const workArea = display.workArea || display.bounds
  const width = Math.min(1100, workArea.width)
  const height = Math.min(800, workArea.height)
  return {
    x: Math.round(workArea.x + (workArea.width - width) / 2),
    y: Math.round(workArea.y + (workArea.height - height) / 2),
    width,
    height,
  }
}

/**
 * Resolve the main chat window without persisting fragile coordinates. It
 * defaults to a normal centered window on the primary display; an explicit
 * display id or `secondary` preference can still move it elsewhere.
 */
export function resolveMainWindowPlacement(
  displays: readonly DisplayLike[],
  primary: DisplayLike,
  preference = 'primary',
  fullscreen = false,
): MainWindowPlacement {
  const selected = requestedDisplay(displays, primary, preference, 'primary')
  const secondary = selected.id !== primary.id
  return {
    bounds: fullscreen ? { ...selected.bounds } : centeredWindowBounds(selected),
    displayId: selected.id,
    fullscreen,
    secondary,
  }
}

/**
 * Give a connected secondary display to the persistent character. If that
 * display disappears, keep the character recoverable as a compact overlay on
 * the primary display instead of leaving a transparent full-screen window over
 * the user's main workspace.
 */
export function resolveFloatingCompanionPlacement(
  displays: readonly DisplayLike[],
  primary: DisplayLike,
  preference = 'secondary',
): FloatingCompanionPlacement {
  const selected = requestedDisplay(displays, primary, preference, 'secondary')
  const dedicatedDisplay = selected.id !== primary.id
  return {
    bounds: dedicatedDisplay
      ? { ...(selected.workArea || selected.bounds) }
      : resolveFloatingCompanionBounds(selected.workArea || selected.bounds),
    dedicatedDisplay,
    displayId: selected.id,
  }
}
