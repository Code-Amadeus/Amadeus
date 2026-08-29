import type { ReviewSummary, WorkPatchHunk, WorkPatchLine, WorkSignalPreview } from './types'
import { mockWorkTurnFixture, type CarryoverNote, type MockWorkboardMode, type TimedCueLine, type TimedWorkNote } from './mockWorkTurn.fixture'

export type { MockWorkboardMode } from './mockWorkTurn.fixture'

export type MockCaptionState = 'fresh' | 'settled' | 'fading'
export type MockWorkCardState = 'expanded' | 'folded'

export type MockWorkboardFrame = {
  mode: MockWorkboardMode
  title: string
  titlePlacement: 'stage' | 'corner'
  kicker: string
  lead: string
  captionLines: Array<Pick<TimedCueLine, 'id' | 'label' | 'text'> & { state: MockCaptionState }>
  carryover?: CarryoverNote
  thoughtLines: Array<Pick<TimedWorkNote, 'id' | 'label' | 'text'>>
  reviewSummary?: ReviewSummary
  summaryLines: Array<Pick<WorkSignalPreview, 'signalId' | 'label' | 'text' | 'files' | 'added' | 'removed' | 'previewKind' | 'detailLabel'> & {
    id: string
    state: MockWorkCardState
    patchLines: WorkPatchLine[]
    currentLine: number
  }>
}

export const MOCK_STAGE_INDEX = mockWorkTurnFixture.stages

function getActiveHunk(preview: WorkSignalPreview, t: number): WorkPatchHunk {
  const age = t - preview.startMs
  return [...preview.hunks]
    .reverse()
    .find(hunk => age >= hunk.startOffsetMs) || preview.hunks[0] || { startOffsetMs: 0, currentLine: 0, patchLines: [] }
}

function frameMode(t: number): MockWorkboardMode {
  if (t < 13800) return 'cue'
  if (t < 42000) return 'active'
  return 'review'
}

function captionState(line: TimedCueLine, t: number): MockCaptionState {
  const age = t - line.startMs
  const remaining = line.endMs - t
  if (age < 700) return 'fresh'
  if (remaining < 1800) return 'fading'
  return 'settled'
}

function visibleReviewSummary(t: number): ReviewSummary {
  const reviewAge = t - MOCK_STAGE_INDEX.find(stage => stage.mode === 'review')!.elapsedMs
  return {
    ...mockWorkTurnFixture.reviewSummary,
    revealCards: reviewAge >= 3900,
    lines: mockWorkTurnFixture.reviewSummary.lines.filter(line => reviewAge >= line.startOffsetMs),
  }
}

export function getMockWorkboardFrame(elapsedMs: number): MockWorkboardFrame {
  const t = elapsedMs % mockWorkTurnFixture.loopMs
  const mode = frameMode(t)
  const visibleCaptions = mockWorkTurnFixture.cueLines
    .filter(line => t >= line.startMs && t < line.endMs)
    .map(line => ({
      id: line.id,
      label: line.label,
      text: line.text,
      state: captionState(line, t),
    }))
    .slice(-3)
  const visibleThoughtLines = mockWorkTurnFixture.workNotes
    .filter(line => t >= line.startMs && t < line.endMs)
    .map(({ id, label, text }) => ({ id, label, text }))
    .slice(-3)
  const startedPreviews = mockWorkTurnFixture.previews
    .filter(preview => mode !== 'cue' && t >= preview.startMs)
    .slice(-5)
  const visibleSummaries = startedPreviews.map((preview, index) => {
    const state: MockWorkCardState = mode === 'review' || index < startedPreviews.length - 1 ? 'folded' : 'expanded'
    const hunk = getActiveHunk(preview, t)
    return {
      id: preview.signalId,
      signalId: preview.signalId,
      label: preview.label,
      text: preview.text,
      files: preview.files,
      added: preview.added,
      removed: preview.removed,
      previewKind: preview.previewKind,
      detailLabel: preview.detailLabel,
      patchLines: hunk.patchLines,
      currentLine: hunk.currentLine,
      state,
    }
  })

  if (mode === 'cue') {
    return {
      mode,
      title: mockWorkTurnFixture.turn.title,
      titlePlacement: 'stage',
      kicker: 'standby',
      lead: 'Waiting for a work request.',
      captionLines: visibleCaptions,
      carryover: mockWorkTurnFixture.carryover,
      thoughtLines: [],
      reviewSummary: undefined,
      summaryLines: [],
    }
  }

  if (mode === 'review') {
    return {
      mode,
      title: mockWorkTurnFixture.turn.title,
      titlePlacement: 'corner',
      kicker: 'review / foldable',
      lead: 'The turn is ready to fold into a capsule after user confirmation.',
      captionLines: [],
      thoughtLines: [],
      reviewSummary: visibleReviewSummary(t),
      summaryLines: visibleSummaries,
    }
  }

  return {
    mode,
    title: mockWorkTurnFixture.turn.title,
    titlePlacement: 'corner',
    kicker: 'active work / streaming summary',
    lead: 'The broadcast has shifted from cinematic cues into stacked work summaries.',
    captionLines: [],
    thoughtLines: visibleThoughtLines,
    reviewSummary: undefined,
    summaryLines: visibleSummaries,
  }
}
