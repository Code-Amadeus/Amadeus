export type Rect = { x: number; y: number; width: number; height: number }

export function clampPanel(bounds: Rect, area: Rect): Rect {
  const width = Math.min(bounds.width, area.width)
  const height = Math.min(bounds.height, area.height)
  return { width, height,
    x: Math.max(area.x, Math.min(bounds.x, area.x + area.width - width)),
    y: Math.max(area.y, Math.min(bounds.y, area.y + area.height - height)),
  }
}

/** Reserve real desktop space; a docked card never covers the game. */
export function dockPanel(game: Rect, area: Rect, width = 470, height = 250): { panel: Rect; game: Rect } {
  const gap = 12
  const panel = clampPanel({ x: game.x + game.width + gap, y: game.y, width, height }, area)
  if (game.x + game.width + gap + panel.width <= area.x + area.width) {
    return { panel, game }
  }
  if (game.x - panel.width - gap >= area.x) {
    return { panel: { ...panel, x: game.x - panel.width - gap }, game }
  }
  const available = area.width - panel.width - gap
  // On narrow displays, place the card below instead of squeezing the game.
  if (available < 640) {
    const next = clampPanel({ ...game, x: area.x, y: area.y,
      width: Math.min(game.width, area.width), height: Math.max(1, area.height - panel.height - gap) }, area)
    return { game: next, panel: { ...panel, x: area.x, y: next.y + next.height + gap } }
  }
  const next = clampPanel({ ...game, x: area.x, width: Math.min(game.width, available) }, area)
  return { game: next, panel: { ...panel, x: next.x + next.width + gap, y: next.y } }
}
