# PR #261 update repair verification

Base: `d17af14f853036cbce9c95bc025d0dd67c66a743` (2026-10-25 review follow-up).

The pending-update repair reuses the existing configuration validator and
settings writer. It invalidates the health receipt before changing settings,
then records the persisted, encrypted target at its new revision. Failed writes
retain the original recovery record and block activation. Missing or unhealthy
projection status remains pending within the same desktop session so configuration
can be repaired; process changes and failed backend starts still require recovery.

The platform repair keeps scheduled installation and retained-version recovery
on Windows. Linux/macOS use the existing immediate, version-isolated installation
path and do not register Windows cleanup owners. The prior POSIX shutdown
contract is preserved; this change does not claim descendant containment there.

## Validation

- `cd electron; npm test`: 670 passed, 0 skipped. An initial translation-key
  ordering failure was corrected before the passing full run.
- `cd electron; npm run build`: passed config/catalog ownership checks, SDK
  verification, Pi type-checking, TypeScript and Vite. Vite retains its existing
  large-chunk warning.
- `.venv/Scripts/python.exe -m pytest tests/test_backend_cleanup.py tests/test_chat_ingress_lifecycle.py tests/test_vts_worker_lifecycle.py tests/test_electron_model_less_smoke.py -q`: 86 passed.
- `.venv/Scripts/python.exe -X utf8 tools/smoke_electron_model_less.py --report-dir build/pr261-smoke`: passed the isolated Windows model-less startup,
  authenticated runtime, plugin/settings navigation and clean-shutdown journey.
- `git diff --check`: passed.

Regression coverage includes required secret/non-secret configuration, stale
health revisions, settings/journal write failures, rollback preserving encrypted
credentials and disable intent, cold recovery, native/catalog IPC routing,
version retention before restart, unsupported scheduling, and platform-specific
renderer actions. Linux/macOS routing is exercised with explicit platform
fixtures on Windows; native Linux/macOS execution was not performed.

## Visual check

These are synthetic static renders of the real React components using the built
stylesheet in headless Chromium on Windows. They verify the guidance layout,
not native Linux/macOS execution or a packaged installer.

- [Linux/macOS before](linux-before.png): incorrectly promises scheduled installation.
- [Linux/macOS after](linux-after.png): explains immediate installation and restart-to-apply.
- [Windows after](windows-after.png): keeps scheduled-installation guidance.

All screenshots use an empty synthetic plugin profile. No user settings,
credentials, conversations, or runtime logs are included.
