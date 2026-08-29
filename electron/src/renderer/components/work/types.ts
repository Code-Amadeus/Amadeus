export interface ProviderEvent {
  provider: string
  run_id: string
  type: string
  payload?: Record<string, unknown>
  metadata?: Record<string, unknown>
  time_ms?: number
}

export interface ProviderRun {
  run_id: string
  provider: string
  task: string
  cwd?: string | null
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled' | 'orphaned'
  result?: string
  error?: string | null
  metadata?: Record<string, unknown>
  events?: ProviderEvent[]
}

export interface ProviderInspectionDetails {
  busy?: string
  error?: string
  diff?: unknown
  status?: unknown
}

export type WorkFocusMode = 'auto' | 'pinned'

export interface WorkDockCounts {
  running: number
  needsAttention: number
  active: number
}

export interface WorkActivitySnapshot {
  phase: string
  elapsedSeconds: number
  silentSeconds: number
  lastEventAt?: string
  lastEventType?: string
  semanticSummary?: string
  lastTool?: string
  toolCount: number
  artifactCount: number
  uncertainty?: string
  liveness?: Record<string, unknown>
  steering?: {
    state?: string
    revision?: number
    safeBoundary?: string
  }
}

export interface WorkDockItem {
  id: string
  title: string
  execution: string
  activity?: WorkActivitySnapshot
  liveness?: string
  livenessStage?: string
  probeStatus?: string
  silentForSeconds?: number
  lastProviderEventAt?: string
  completion: string
  attention: string
  workspaceLabel: string
  workspacePath?: string
  workspaceMode?: string
  branch?: string
  isolation?: string
  selectionReason?: string
  writerLeaseStatus?: string
  artifactCount?: number
  workspaceExists?: boolean
  updatedAt: string
  projectId?: string
  projectName?: string
  projectState?: string
  canRetry?: boolean
  canResume?: boolean
  canReopen?: boolean
  isScratch?: boolean
  canPromoteToProject?: boolean
  retryAuthorizationRequestId?: string
  currentRunId?: string
  sessionId?: string
  state?: string
  provider?: string
  mode?: string
}

export interface WorkProjectSummary {
  id: string
  name: string
  latestWorkItemId?: string
  latestTaskTitle?: string
  current: number
  running: number
  actionRequired: number
  history: number
}

export interface WorkProjection {
  revision: string
  currentSessionId?: string
  selectedWorkItemId: string
  focusMode: WorkFocusMode
  workspaceFocusMode: WorkFocusMode
  workspaceFocusPath?: string
  workspaceFocusWorkItemId?: string
  destinationLabel?: string
  destinationFeedback?: {
    status: string
    message: string
  }
  counts: WorkDockCounts
  projects: WorkProjectSummary[]
  items: WorkDockItem[]
}

export interface AttentionOption {
  id: string
  label: string
  entityKind: 'project' | 'work_item' | 'other'
  description?: string
  parentLabel?: string
  metadata?: Record<string, string | number | boolean>
}

export interface AttentionRequest {
  schemaId: string
  id: string
  sessionId: string
  kind: 'selection'
  status: 'pending'
  title: string
  prompt: string
  options: AttentionOption[]
  createdAt: number
  expiresAt: number
}

export interface WorkPageProps {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
  connected: boolean
}

export interface AuipExperienceProjection {
  artifactRef: string
  appSessionId?: string
  title: string
  status: 'connecting' | 'active' | 'completed' | 'disconnected' | 'closed'
  stance?: string
  engagementMode?: 'observe' | 'collaborate' | 'delegate'
  operatorStatus?: 'idle' | 'thinking' | 'awaiting_receipt' | 'error'
  operatorError?: string
  latestEvent?: string
  latestAction?: string
  latestNarration?: string
  terminal?: string
}

export type OverlayMode = 'none' | 'canvas' | 'trace' | 'permission' | 'attention' | 'audit'
export type TurnPhase = 'standby' | 'orienting' | 'working' | 'reviewing' | 'complete' | 'blocked'
export type RichTone = 'neutral' | 'info' | 'success' | 'warning' | 'attention' | 'done'

export interface RichAction {
  label: string
  onClick?: () => void
}

export type WorkTurnStatus = 'planning' | 'running' | 'blocked' | 'review' | 'accepted' | 'reverted'
export type WorkTurnPhase = 'intake' | 'contract' | 'plan' | 'inspect' | 'edit' | 'validate' | 'review'
export type WorkSignalPhase = 'plan' | 'inspect' | 'edit' | 'validate' | 'permission' | 'error' | 'result'
export type WorkSignalImportance = 'ambient' | 'normal' | 'important' | 'blocking'

export interface SignalRef {
  type: 'file' | 'diff' | 'test' | 'command' | 'artifact' | 'permission' | 'provider'
  label: string
  ref: string
}

export interface WorkSignal {
  id: string
  turnId: string
  phase: WorkSignalPhase
  importance: WorkSignalImportance
  title: string
  summary: string
  evidence?: SignalRef[]
  rawEventRefs: string[]
  expandable: boolean
}

export type WorkPatchLineKind = 'add' | 'remove' | 'context' | 'terminal' | 'success' | 'warn'
export type WorkPreviewKind = 'diff' | 'terminal'

export interface WorkPatchLine {
  kind: WorkPatchLineKind
  line: number
  text: string
}

export interface WorkPatchHunk {
  startOffsetMs: number
  currentLine: number
  patchLines: WorkPatchLine[]
}

export interface WorkSignalPreview {
  signalId: string
  startMs: number
  label: string
  text: string
  previewKind?: WorkPreviewKind
  detailLabel?: string
  files: string[]
  added: number
  removed: number
  hunks: WorkPatchHunk[]
}

export interface ReviewSummaryLine {
  id: string
  startOffsetMs: number
  text: string
  evidence: string[]
}

export interface ReviewSummary {
  revealCards: boolean
  lines: ReviewSummaryLine[]
  files: Array<{
    path: string
    label: string
    added: number
    removed: number
    note: string
  }>
  validation: string[]
  watchpoints: string[]
  nextActions: string[]
}

export interface EvidenceItem {
  id: string
  kind: 'file' | 'diff' | 'command' | 'test' | 'artifact'
  title: string
  summary: string
  path?: string
  command?: string
  artifactId?: string
}

export interface ValidationResult {
  id: string
  kind: 'typecheck' | 'test' | 'build' | 'lint' | 'manual'
  status: 'pending' | 'running' | 'passed' | 'failed' | 'skipped'
  summary: string
  command?: string
}

export interface RiskNote {
  id: string
  level: 'low' | 'medium' | 'high'
  summary: string
  mitigation?: string
}

export interface UserIntervention {
  id: string
  source: 'voice' | 'text' | 'button' | 'file'
  status: 'received' | 'queued' | 'injected' | 'conflict'
  summary: string
  createdAt: number
}

export interface PermissionRequest {
  id: string
  action: string
  provider: string
  riskLevel: 'low' | 'medium' | 'high'
  scope: string
  status: 'pending' | 'allowed' | 'denied' | 'expired'
  reason?: string
  reversibility?: string
}

export interface ArtifactRef {
  id: string
  kind: 'markdown' | 'table' | 'chart' | 'html' | 'diff' | 'image' | 'file' | 'terminal' | 'schema' | 'commit'
  title: string
  ref: string
}

export interface WorkTurn {
  id: string
  title: string
  intent: string
  status: WorkTurnStatus
  phase: WorkTurnPhase
  provider: string
  branch?: string
  worktree?: string
  summary: string
  signals: WorkSignal[]
  evidence: EvidenceItem[]
  validation: ValidationResult[]
  risks: RiskNote[]
  pendingInputs: UserIntervention[]
  permissions: PermissionRequest[]
  artifacts: ArtifactRef[]
}

export interface WorkTurnNode {
  id: string
  title: string
  status: WorkTurnStatus
  provider: string
  changedFiles: number
  validation: 'pending' | 'running' | 'passed' | 'failed' | 'skipped'
  artifactCount: number
}

export interface TurnEdge {
  from: string
  to: string
  kind: 'sequence' | 'depends_on' | 'branch'
}

export interface ProjectStateMap {
  currentTurnId: string
  turns: WorkTurnNode[]
  edges: TurnEdge[]
}
