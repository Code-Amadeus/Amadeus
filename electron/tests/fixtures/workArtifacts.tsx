import React, { useState } from 'react'
import ReactDOM from 'react-dom/client'
import ProjectAppsPanel from '../../src/renderer/components/ProjectAppsPanel'
import type { WorkFileSummary } from '../../src/renderer/components/WorkFilesPanel'
import '../../src/renderer/styles/index.css'

const host = window as any
host.artifactFixture = { reads: [], opened: [], missing: false, apps: 0 }
const f = host.artifactFixture
const files: WorkFileSummary[] = ['audit.json', 'orders.csv', 'issues.txt', 'notes.md'].map((title, index) => ({
  projectId: 'draft-project', workItemId: 'audit-work', workTitle: 'Audit orders',
  workState: 'review_ready', execution: 'succeeded', artifactId: `file-${index}`,
  title, relativePath: title, artifactStatus: 'registered', sizeBytes: 12, previewable: true,
}))
const contents = ['{"rows":5}', 'id,amount\nA,10', 'row 2: negative amount', '<script>window.attacked = true</script>\n# Plain Markdown']
files.push({ ...files[0], artifactId: 'external-report', title: 'exported-report.md',
  relativePath: 'exported-report.md', artifactStatus: 'approved', previewable: false,
  artifactKind: 'business.export' })
const readFile = async file => {
  f.reads.push({ work_item_id: file.workItemId, artifact_id: file.artifactId })
  if (f.pause) return new Promise<Record<string, unknown>>(resolve => { f.release = () => resolve({ ok: true, content: 'Late response' }) })
  if (file.artifactId === 'manifest') return { ok: true, content: '{"schema":"auip/v0"}' }
  return f.missing ? { ok: false, error: 'File is missing or no longer inside its WorkItem workspace.' }
    : { ok: true, content: contents[files.indexOf(file)] }
}
function Fixture() {
  const [refresh, setRefresh] = useState(0)
  const [, setPresentation] = useState(0)
  f.renders = (f.renders || 0) + 1
  f.rerender = () => setPresentation(value => value + 1)
  return <ProjectAppsPanel key={refresh} project={{ projectId: 'drafts', name: 'Draft artifacts' }} scope="drafts"
    apps={[{ projectId: 'draft-project', workItemId: 'app-work', workTitle: 'Interactive demo', artifactId: 'app', artifactRef: 'artifact:app@abc',
      appId: 'demo', title: 'Verified application', version: '1', modes: ['observe'], revision: 1, workState: 'review_ready', execution: 'succeeded', files: [{ artifactId: 'manifest', title: 'auip.manifest.json', relativePath: 'auip.manifest.json', artifactStatus: 'registered', previewable: true }] }]}
    files={files} readFile={readFile} appsComplete filesComplete loading={false} actionKey="" feedback="" onBack={() => {}} onNewChat={() => {}}
    onRefresh={() => setRefresh(value => value + 1)} onOpen={app => f.opened.push(app.artifactId)} onInteract={() => f.apps++} />
}
ReactDOM.createRoot(document.getElementById('root')!).render(<Fixture />)
