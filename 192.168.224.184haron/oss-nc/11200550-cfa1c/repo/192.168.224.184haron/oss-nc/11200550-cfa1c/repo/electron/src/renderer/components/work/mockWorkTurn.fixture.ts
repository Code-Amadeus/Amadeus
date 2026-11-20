import type { ReviewSummary, WorkSignalPreview, WorkTurn } from './types'

export type MockWorkboardMode = 'cue' | 'active' | 'review'

export type TimedCueLine = {
  id: string
  startMs: number
  endMs: number
  label: string
  text: string
}

export type TimedWorkNote = {
  id: string
  startMs: number
  endMs: number
  label: string
  text: string
}

export type CarryoverNote = {
  title: string
  items: string[]
}

export type MockWorkTurnFixture = {
  loopMs: number
  turn: WorkTurn
  stages: Array<{
    mode: MockWorkboardMode
    label: string
    elapsedMs: number
  }>
  cueLines: TimedCueLine[]
  workNotes: TimedWorkNote[]
  carryover: CarryoverNote
  previews: WorkSignalPreview[]
  reviewSummary: ReviewSummary
}

const turnId = 'mock-turn-focused-work'

export const mockWorkTurnFixture: MockWorkTurnFixture = {
  loopMs: 51500,
  turn: {
    id: turnId,
    title: 'Start a focused work turn',
    intent: 'Prototype the Amadeus turn-based work surface as a provider-ready interaction model.',
    status: 'running',
    phase: 'edit',
    provider: 'codex',
    summary: 'Waiting for a work request.',
    signals: [
      {
        id: 'signal-layout',
        turnId,
        phase: 'edit',
        importance: 'important',
        title: 'Refined active work layout',
        summary: 'Turn title moved into the upper-left ribbon once active work begins.',
        evidence: [
          { type: 'file', label: 'WorkPage.tsx', ref: 'electron/src/renderer/components/WorkPage.tsx' },
          { type: 'file', label: 'index.css', ref: 'electron/src/renderer/styles/index.css' },
        ],
        rawEventRefs: ['mock:event:layout'],
        expandable: true,
      },
      {
        id: 'signal-build',
        turnId,
        phase: 'validate',
        importance: 'important',
        title: 'Renderer build passed',
        summary: 'Ran the renderer build after the first UI change and captured the result.',
        evidence: [{ type: 'command', label: 'npm run build', ref: 'npm run build' }],
        rawEventRefs: ['mock:event:build'],
        expandable: true,
      },
      {
        id: 'signal-plan',
        turnId,
        phase: 'edit',
        importance: 'normal',
        title: 'Plan drawer stays folded',
        summary: 'The plan timeline stays in a hover drawer, keeping the CRT surface readable.',
        evidence: [{ type: 'file', label: 'index.css', ref: 'electron/src/renderer/styles/index.css' }],
        rawEventRefs: ['mock:event:plan'],
        expandable: true,
      },
      {
        id: 'signal-review',
        turnId,
        phase: 'result',
        importance: 'important',
        title: 'Review summary ready',
        summary: 'The turn is ready to fold into a capsule after user confirmation.',
        evidence: [{ type: 'artifact', label: 'review summary', ref: 'mock:review' }],
        rawEventRefs: ['mock:event:review'],
        expandable: true,
      },
    ],
    evidence: [
      {
        id: 'evidence-workpage',
        kind: 'file',
        title: 'WorkPage.tsx',
        summary: 'Workboard stage rendering, summary sequencing, and review actions.',
        path: 'electron/src/renderer/components/WorkPage.tsx',
      },
      {
        id: 'evidence-playback',
        kind: 'file',
        title: 'mockWorkboardPlayback.ts',
        summary: 'Timed playback adapter for the fixture model.',
        path: 'electron/src/renderer/components/work/mockWorkboardPlayback.ts',
      },
      {
        id: 'evidence-css',
        kind: 'file',
        title: 'index.css',
        summary: 'CRT review surface and progressive disclosure styling.',
        path: 'electron/src/renderer/styles/index.css',
      },
    ],
    validation: [
      {
        id: 'validation-build',
        kind: 'build',
        status: 'passed',
        summary: 'Renderer build passed.',
        command: 'npm run build',
      },
    ],
    risks: [
      {
        id: 'risk-diff-wiring',
        level: 'medium',
        summary: 'File buttons still need real diff inspector wiring.',
        mitigation: 'Keep raw trace and audit available until the inspector is connected.',
      },
    ],
    pendingInputs: [],
    permissions: [],
    artifacts: [
      { id: 'artifact-review', kind: 'markdown', title: 'Turn summary', ref: 'mock:review' },
      { id: 'artifact-terminal', kind: 'terminal', title: 'Renderer build', ref: 'mock:terminal' },
    ],
  },
  stages: [
    { mode: 'cue', label: 'Cue', elapsedMs: 0 },
    { mode: 'active', label: 'Work', elapsedMs: 14200 },
    { mode: 'review', label: 'Summary', elapsedMs: 42000 },
  ],
  cueLines: [
    {
      id: 'cue-ready',
      startMs: 0,
      endMs: 7200,
      label: 'ready',
      text: 'Start with a short request. The CRT will keep the center focused on this turn, not the whole IDE.',
    },
    {
      id: 'cue-scope',
      startMs: 2600,
      endMs: 9800,
      label: 'scope',
      text: 'I will keep provider runtime details folded unless a permission, failure, or review point needs attention.',
    },
    {
      id: 'cue-surface',
      startMs: 5200,
      endMs: 12400,
      label: 'surface',
      text: 'Voice carries intent. The workboard keeps concise evidence close enough to inspect.',
    },
    {
      id: 'cue-risk',
      startMs: 8400,
      endMs: 13800,
      label: 'watch',
      text: 'If the turn starts behaving like a code log, collapse it back into summary and trace.',
    },
  ],
  workNotes: [
    {
      id: 'post-build-note-fold',
      startMs: 31500,
      endMs: 35400,
      label: 'result',
      text: 'build passed - folding terminal evidence into the turn',
    },
    {
      id: 'post-build-note-classify',
      startMs: 32200,
      endMs: 35400,
      label: 'route',
      text: 'checking whether the next change belongs in the plan drawer or the workboard surface',
    },
    {
      id: 'post-build-note-next',
      startMs: 32900,
      endMs: 35400,
      label: 'next',
      text: 'preparing the next visible card only after the validation signal is stable',
    },
  ],
  carryover: {
    title: 'Previous turn checkpoint',
    items: [
      '`run` card now captures terminal evidence instead of pretending every step is a file edit.',
      '`layout` card stays file-scoped and folds into a compact delta after its hunks finish.',
      'Next: keep the visible workboard tied to real provider signals, not decorative logs.',
    ],
  },
  previews: [
    {
      signalId: 'signal-layout',
      startMs: 14200,
      label: 'layout',
      text: 'Turn title moved into the upper-left ribbon once active work begins.',
      previewKind: 'diff',
      files: ['electron/src/renderer/components/WorkPage.tsx', 'electron/src/renderer/styles/index.css'],
      added: 44,
      removed: 18,
      hunks: [
        {
          startOffsetMs: 0,
          currentLine: 397,
          patchLines: [
            { kind: 'context', line: 392, text: '<section className="crt-workboard-stage">' },
            { kind: 'remove', line: 397, text: '<h1>{activeTurn.title}</h1>' },
            { kind: 'remove', line: 398, text: '<p>{activeTurn.summary}</p>' },
            { kind: 'add', line: 397, text: '<h2 className="crt-board-title">{displayTitle}</h2>' },
            { kind: 'add', line: 398, text: '<p className="crt-turn-voice-line">{displayLead}</p>' },
            { kind: 'context', line: 399, text: '</section>' },
          ],
        },
        {
          startOffsetMs: 4200,
          currentLine: 447,
          patchLines: [
            { kind: 'context', line: 444, text: '<div className={mockFrame ? "crt-summary-stack" : "crt-turn-feed"}>' },
            { kind: 'remove', line: 447, text: '<div className="crt-turn-line">' },
            { kind: 'add', line: 447, text: '{displayNarrationItems.map((item, index) => (' },
            { kind: 'add', line: 448, text: '<div className={`crt-summary-line ${item.state || "expanded"}`}>' },
            { kind: 'context', line: 451, text: '{mockFrame && item.files && typeof item.added === "number" && (' },
            { kind: 'add', line: 452, text: '<small className="crt-summary-delta" title={item.files.join("\\n")}>' },
          ],
        },
        {
          startOffsetMs: 7600,
          currentLine: 456,
          patchLines: [
            { kind: 'context', line: 455, text: ')}' },
            { kind: 'add', line: 456, text: '{mockFrame?.mode === "active" && item.state === "expanded" && item.patchLines && (' },
            { kind: 'add', line: 457, text: '<div className="crt-patch-stream" aria-label="Streaming patch preview">' },
            { kind: 'add', line: 458, text: '<div className="crt-patch-line-indicator">{item.currentLine}</div>' },
            { kind: 'context', line: 460, text: '{item.patchLines.map((line, lineIndex) => (' },
            { kind: 'add', line: 462, text: '<span>{line.line}</span>' },
            { kind: 'add', line: 463, text: '<b>{formatPreviewPrefix(line.kind)}{line.text}</b>' },
          ],
        },
      ],
    },
    {
      signalId: 'signal-build',
      startMs: 25200,
      label: 'run',
      text: 'Ran the renderer build after the first UI change and captured the result.',
      previewKind: 'terminal',
      detailLabel: 'npm run build - exit 0 / 4.6s',
      files: ['npm run build'],
      added: 0,
      removed: 0,
      hunks: [
        {
          startOffsetMs: 0,
          currentLine: 0,
          patchLines: [
            { kind: 'terminal', line: 1, text: '> amadeus-desktop@0.1.0 build' },
            { kind: 'terminal', line: 2, text: '> tsc && vite build' },
            { kind: 'terminal', line: 3, text: 'vite v6.4.2 building for production...' },
            { kind: 'terminal', line: 4, text: 'transforming modules...' },
            { kind: 'terminal', line: 5, text: 'renderer compile started' },
          ],
        },
        {
          startOffsetMs: 2500,
          currentLine: 0,
          patchLines: [
            { kind: 'terminal', line: 1, text: '> amadeus-desktop@0.1.0 build' },
            { kind: 'terminal', line: 2, text: '> tsc && vite build' },
            { kind: 'success', line: 6, text: '232 modules transformed.' },
            { kind: 'terminal', line: 7, text: 'rendering chunks...' },
            { kind: 'terminal', line: 8, text: 'computing gzip size...' },
            { kind: 'success', line: 9, text: 'dist/renderer/assets/index.css  51.74 kB' },
          ],
        },
        {
          startOffsetMs: 5200,
          currentLine: 0,
          patchLines: [
            { kind: 'success', line: 6, text: '232 modules transformed.' },
            { kind: 'success', line: 9, text: 'dist/renderer/assets/index.css  51.74 kB' },
            { kind: 'success', line: 10, text: 'dist/renderer/assets/index.js  488.31 kB' },
            { kind: 'success', line: 11, text: 'built in 1.24s' },
            { kind: 'terminal', line: 12, text: 'exit code 0' },
            { kind: 'terminal', line: 13, text: 'validation signal: renderer build passed' },
          ],
        },
      ],
    },
    {
      signalId: 'signal-plan',
      startMs: 35600,
      label: 'plan',
      text: 'The plan timeline stays in a hover drawer, keeping the CRT surface readable.',
      previewKind: 'diff',
      files: ['electron/src/renderer/styles/index.css'],
      added: 31,
      removed: 9,
      hunks: [
        {
          startOffsetMs: 0,
          currentLine: 745,
          patchLines: [
            { kind: 'context', line: 739, text: '.crt-focus-layout {' },
            { kind: 'remove', line: 740, text: '  grid-template-columns: minmax(230px, 300px) minmax(0, 1fr);' },
            { kind: 'add', line: 740, text: '  grid-template-columns: minmax(0, 1fr);' },
            { kind: 'add', line: 745, text: '.crt-task-dock { position: absolute; left: 24px; width: 42px; }' },
            { kind: 'add', line: 783, text: '.crt-task-dock:hover .crt-timeline-compact { opacity: 1; transform: translateX(0); }' },
          ],
        },
      ],
    },
    {
      signalId: 'signal-review',
      startMs: 42000,
      label: 'review',
      text: 'The turn is ready to fold into a capsule after user confirmation.',
      previewKind: 'diff',
      files: ['docs/ai_os_turn_workboard_design.md', 'electron/src/renderer/components/work/mockWorkboardPlayback.ts'],
      added: 24,
      removed: 4,
      hunks: [
        {
          startOffsetMs: 0,
          currentLine: 56,
          patchLines: [
            { kind: 'context', line: 50, text: 'Current Turn title, cue notes, active summaries, evidence, validation, review' },
            { kind: 'add', line: 56, text: 'The global top layer remains reserved for goal / plan / timeline orientation.' },
            { kind: 'context', line: 57, text: 'The current turn title belongs inside the workboard stage.' },
          ],
        },
      ],
    },
  ],
  reviewSummary: {
    revealCards: false,
    lines: [
      {
        id: 'review-line-shape',
        startOffsetMs: 0,
        text: 'This turn tightened the workboard into a clearer three-part rhythm: cue, visible work, then review.',
        evidence: ['stage index', 'WorkPage.tsx'],
      },
      {
        id: 'review-line-work',
        startOffsetMs: 900,
        text: 'The active phase now shows file edits and terminal validation as separate evidence instead of flattening everything into one log stream.',
        evidence: ['patch preview', 'npm run build'],
      },
      {
        id: 'review-line-fold',
        startOffsetMs: 1850,
        text: 'At the end of the turn, the detailed evidence folds down into a smaller review surface so the CRT can stay readable.',
        evidence: ['file index', 'validation'],
      },
      {
        id: 'review-line-next',
        startOffsetMs: 2850,
        text: 'Next step: replace this mock playback with real provider WorkSignals and wire the file buttons into a diff inspector.',
        evidence: ['WorkSignal', 'diff inspector'],
      },
    ],
    files: [
      {
        path: 'electron/src/renderer/components/WorkPage.tsx',
        label: 'WorkPage.tsx',
        added: 58,
        removed: 14,
        note: 'Review summary layout, vertical working notes, cue carryover.',
      },
      {
        path: 'electron/src/renderer/components/work/mockWorkboardPlayback.ts',
        label: 'mockWorkboardPlayback.ts',
        added: 72,
        removed: 18,
        note: 'Mock timing, terminal run card, carryover and review model.',
      },
      {
        path: 'electron/src/renderer/styles/index.css',
        label: 'index.css',
        added: 96,
        removed: 20,
        note: 'CRT review surface, file index buttons, validation blocks.',
      },
    ],
    validation: [
      'Renderer build passed.',
      'Terminal result is visible as evidence.',
      'Review keeps file entry points below the summary.',
    ],
    watchpoints: [
      'File buttons still need real diff inspector wiring.',
      'Review should stay compact unless the user expands details.',
    ],
    nextActions: [
      'Open diff',
      'Accept turn',
      'Ask for refinement',
      'Archive capsule',
    ],
  },
}
