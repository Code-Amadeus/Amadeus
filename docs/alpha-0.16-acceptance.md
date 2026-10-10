# 0.16.0 Alpha acceptance record

Date: 2026-10-11 (Pacific/Auckland). Preparation baseline: `09aecfb`, including
merged #173 and #174. Comparison release: `v0.15.2-alpha.0`.

Release preparation uses a clean, isolated Git worktree. Uncommitted local voice
configuration, private runtime state and external media are excluded.

## Current validation

Validation is in progress. This record will be updated with observed results
before publication. Historical feature records are not counted as a pass for
the final release candidate.

## Evidence boundaries

Deterministic suites, native desktop startup, extracted source installation,
real-model interaction, device playback and human microphone/listening checks
establish different facts. Missing optional dependencies and manual checks are
reported as skipped or unverified, never counted as passes. Raw logs, local
paths, credentials, conversations and runtime media remain outside the source
release; only sanitized aggregate results are recorded here.
