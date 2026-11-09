# PR #261 update-area UI verification

The update section now uses the existing `SettingsGroup`, `CardShell`, `Switch`,
`SettingsButton` and `StatusPill` presentation. Automatic checking and manual
security checks occupy separate settings rows. Scheduled installation and
pending/recovery state share one status card. Package digests, retained-state
details and installation policy remain available in disclosures rather than
dominating the page. Actions and host-side update authority are unchanged.

## Checks

- `cd electron; npm test`: 673 passed, 0 skipped.
- `cd electron; npm run build`: passed; the existing Vite large-chunk warning remains.
- `cd electron; node -e "require('node:child_process').execFileSync(require('electron'), ['--disable-gpu','tests/extensionSettings.smoke.mjs'], {stdio:'inherit',windowsHide:true,timeout:120000})"`: passed, 12 interaction checks.
- `python docs/evidence/pr261-update-ui/capture.py`: passed the isolated model-less
  app smoke and real-window captures. The final capture also asserts no error
  banners and no horizontal overflow in the settings/status surfaces.
- `git diff --check`, the store smoke's JavaScript syntax check and the changed
  Python probe syntax checks passed. The full store/package-install journey was
  not qualified in this UI follow-up.

The first direct PowerShell GUI-test launch lost its output pipes and displayed
an `EPIPE` dialog. Those disposable test processes were closed, and the settings
smoke was rerun using the CI Node wrapper above. That failed launch is not counted
as a passing test. No production exception suppression was added. A first capture
attempt also used an ambiguous locator for combined rollback/recovery state;
the final capture selects the recovery section explicitly.

## Real application screenshots

These images come from the built Electron application, with its normal app shell,
theme provider and model-less backend, using an isolated synthetic user profile.
The default view uses the actual empty profile. Scheduled, ready and recovery
states use synthetic catalog replies injected only into the owned disposable
test process. They demonstrate UI states, not completed plugin installations.

- [Default Chinese view](live-default-zh.png)
- [Scheduled installation](live-scheduled-zh.png)
- [Ready to confirm, light theme](live-ready-zh.png)
- [Ready to confirm, dark theme](live-ready-dark-zh.png)
- [Combined rollback/recovery at narrow width](live-recovery-narrow-zh.png)

Default client viewport: 1099 x 801 CSS pixels. Narrow client viewport: 599 x 701.
Screenshots are captured at CSS scale. Both views retain vertical scrolling as
needed; no horizontal overflow was observed. User settings, credentials and
conversations are not used or included.
