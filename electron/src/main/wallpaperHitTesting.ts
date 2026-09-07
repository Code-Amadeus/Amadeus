type Point = { x: number; y: number }
type Rectangle = { x: number; y: number; width: number; height: number }

export function desktopPointHitsWindowRegions(
  point: Point,
  windowBounds: Rectangle,
  regions: Rectangle[],
): boolean {
  return regions.some(region => {
    if (region.width <= 0 || region.height <= 0) return false
    const left = windowBounds.x + region.x
    const top = windowBounds.y + region.y
    return point.x >= left
      && point.x <= left + region.width
      && point.y >= top
      && point.y <= top + region.height
  })
}
