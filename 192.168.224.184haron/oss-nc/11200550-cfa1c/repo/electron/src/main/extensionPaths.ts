import fs from 'node:fs'
import path from 'node:path'

/** The installation root and its descendants must not be links or junctions. */
export function assertExtensionPath(root: string, target: string): void {
  const absoluteRoot = path.resolve(root)
  const absoluteTarget = path.resolve(target)
  const relative = path.relative(absoluteRoot, absoluteTarget)
  if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error('Extension filesystem target is outside its installation directory')
  }
  let current = absoluteRoot
  if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) throw new Error('Extension installation root must not be a link or junction')
  for (const segment of relative.split(path.sep)) {
    current = path.join(current, segment)
    if (!fs.existsSync(current)) continue
    if (fs.lstatSync(current).isSymbolicLink()) throw new Error('Extension filesystem paths must not contain links or junctions')
  }
}
