/** Project expansion shows identities together; it never paginates the graph.
 * Card text remains original content. Density changes its viewport, not data. */
export type MapMember = { id: string; parentTaskId?: string; contextOnly?: boolean }
export type MapBox = { x: number; y: number; width: number; height: number }
type MapNode = MapBox & { id: string }
const overlap = (a: MapBox, b: MapBox, gap = 0) => a.x < b.x + b.width + gap && b.x < a.x + a.width + gap
  && a.y < b.y + b.height + gap && b.y < a.y + a.height + gap
const intersection = (a: MapBox, b: MapBox) => Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x))
  * Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y))

export function projectFamilies(members: MapMember[]) {
  const result: MapMember[][] = [], visited = new Set<string>()
  const visit = (member: MapMember, family: MapMember[]) => {
    if (visited.has(member.id)) return
    visited.add(member.id); family.push(member)
    members.filter(child => child.parentTaskId === member.id).sort((a,b) => a.id.localeCompare(b.id)).forEach(child => visit(child, family))
  }
  for (const root of members.filter(member => !members.some(parent => parent.id === member.parentTaskId))) {
    const family: MapMember[] = []; visit(root, family); result.push(family)
  }
  for (const member of members) if (!visited.has(member.id)) {
    const family: MapMember[] = []; visit(member, family); result.push(family)
  }
  return result
}

/** Miniature cards describe topology instead of shrinking unreadable body text.
 * Every local identity has a tile; titles remain available on hover and click. */
export function projectMiniature(members: MapMember[], width: number) {
  const nodes: MapNode[] = [], tile = 42, gap = 9
  let y = 0
  for (const family of projectFamilies(members)) {
    const root = family[0]
    nodes.push({ id: root.id, x: 0, y, width: tile, height: 22 })
    const nextY = y + 31
    family.slice(1).forEach((member, index) => {
      const columns = Math.max(1, Math.floor((width - 20 + gap) / (tile + gap)))
      nodes.push({ id: member.id, x: 20 + index % columns * (tile + gap), y: nextY + Math.floor(index / columns) * 31, width: tile, height: 22 })
    })
    y = Math.max(y + 22, ...nodes.map(node => node.y + node.height)) + 17
  }
  return { nodes, height: Math.max(0, y - 17) }
}

function familyShape(members: MapMember[], width: number, height: number, columns: number) {
  const nodes: MapNode[] = []
  if (columns === 1) {
    let y = 0
    for (const member of members) {
      const parent = nodes.find(node => node.id === member.parentTaskId)
      const x = parent ? Math.min(36, parent.x + 18) : 0
      const h = member.contextOnly ? 42 : height
      nodes.push({ id: member.id, x, y, width, height: h }); y += h + 24
    }
  } else {
    // A broad family owns a complete block. Its children never occupy another
    // root's lane merely because a flattened grid happens to have an empty cell.
    nodes.push({ id: members[0].id, x: (width + 28) * (columns - 1) / 2, y: 0, width, height: members[0].contextOnly ? 42 : height })
    members.slice(1).forEach((member, index) => nodes.push({ id: member.id,
      x: index % columns * (width + 28), y: nodes[0].height + 28 + Math.floor(index / columns) * (height + 24),
      width, height: member.contextOnly ? 42 : height }))
  }
  return { nodes, width: Math.max(...nodes.map(node => node.x + node.width)), height: Math.max(...nodes.map(node => node.y + node.height)) }
}

export function projectOverviewMap(members: MapMember[], width: number, height: number, obstacles: MapBox[], character?: MapBox) {
  if (character && !overlap(character, { x: 0, y: 0, width, height })) character = undefined
  const families = projectFamilies(members).sort((a, b) => b.length - a.length || a[0].id.localeCompare(b[0].id))
  const core = character && { x: character.x + character.width * .1, y: character.y + Math.min(60, character.height * .06),
    width: character.width * .8, height: character.height - Math.min(60, character.height * .06) }
  for (const density of [{ width: 240, height: 188 }, { width: 220, height: 174 }, { width: 220, height: 160 }]) {
    for (const borrowEdges of [false, true]) {
      const nodes: MapNode[] = [], blocks: MapBox[] = [...obstacles]
      let borrowed = 0, complete = true
      for (const family of families) {
        let best: { shape: ReturnType<typeof familyShape>; x: number; y: number; cost: number; borrowed: number } | undefined
        for (const columns of family.length > 3 ? [1, 2, 3] : [1]) {
          const shape = familyShape(family, Math.min(density.width, width - 40), density.height, columns)
          for (let y = 120; y + shape.height <= height - 28; y += 18) for (let x = 12; x + shape.width <= width - 12; x += 18) {
            const box = { x, y, width: shape.width, height: shape.height }
            if (blocks.some(other => overlap(box, other, 22))) continue
            if (character && overlap(box, borrowEdges ? core! : character, 12)) continue
            const loan = character ? intersection(box, character) : 0
            if (character && borrowed + loan > character.width * character.height * .12) continue
            const cost = y + x * .06 + loan * .02 + (columns > 1 ? 65 : 0)
            if (!best || cost < best.cost) best = { shape, x, y, cost, borrowed: loan }
          }
        }
        if (!best) { complete = false; break }
        for (const node of best.shape.nodes) nodes.push({ ...node, x: node.x + best.x, y: node.y + best.y })
        blocks.push({ x: best.x, y: best.y, width: best.shape.width, height: best.shape.height })
        borrowed += best.borrowed
      }
      if (complete) return { nodes, scale: 1, borrowed }
    }
  }
  // Beyond readable one-screen capacity, preserve every identity in a fitted
  // map rather than silently splitting it into pages or dropping connections.
  const availableWidth = Math.max(80, Math.min(width - 24, ...obstacles.map(box => box.x - 24)) - 12)
  const availableHeight = Math.max(80, Math.min(height - 28, character?.y ?? height) - 144)
  const shapes = families.map(family => familyShape(family, 240, 174, family.length > 3 ? 3 : 1))
  let best: { nodes: MapNode[]; scale: number } | undefined
  // Fit the same family blocks, retaining their parent/child neighbourhoods.
  // This is only for loads beyond readable one-screen capacity.
  for (let columns = 1; columns <= Math.max(1, shapes.length); columns++) {
    const columnWidth = Math.max(240, ...shapes.map(shape => shape.width)) + 32
    const bottoms = Array.from({ length: columns }, () => 0), nodes: MapNode[] = []
    for (const shape of shapes) {
      const column = bottoms.indexOf(Math.min(...bottoms)), x = column * columnWidth, y = bottoms[column]
      nodes.push(...shape.nodes.map(node => ({ ...node, x: node.x + x, y: node.y + y })))
      bottoms[column] += shape.height + 32
    }
    const scale = Math.min(1, availableWidth / (columns * columnWidth - 32), availableHeight / (Math.max(...bottoms) - 32))
    if (!best || scale > best.scale) best = { nodes, scale }
  }
  return { nodes: best!.nodes.map(node => ({ ...node, x: 12 + node.x * best!.scale, y: 120 + node.y * best!.scale })),
    scale: best!.scale, borrowed: 0 }
}
