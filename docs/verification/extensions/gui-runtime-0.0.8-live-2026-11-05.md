# Windows GUI Runtime Connector 0.0.8 independent live acceptance

The bounded installed-Pi GUI journey passed with connector **0.0.8** and Runtime
**0.1.11** on 2026-11-05 (Pacific/Auckland). This closes the outstanding four-step
GUI, original-image review, same-session observe/continue and maintenance-settlement
checks. It is evidence for the exact combination, not catalog approval or publication.

Host connection diagnostics were tested with the local fixes accompanying this
record, based on `ffb1037b6a4cc8bcbf768d51111b108361952cb0`. Those fixes must be
reviewed and integrated before describing the diagnostic experience as shipped.

## Tested bytes and environment

- Submission: [Amadeus-Extensions #11](https://github.com/Code-Amadeus/Amadeus-Extensions/pull/11),
  head `8705a3f04e602d145d74782c310f6866cbc20f24`, still open when checked.
- Wheel: `amadeus_agent_gui_runtime-0.0.8-py3-none-any.whl`, 27,990 bytes,
  SHA-256 `2f532faf152faf2e1e3ab3140e1097487689be34d638e4ee86539e06886cbcaf`.
- Runtime: the existing installation was reused at unmodified tracked source
  `323099100b04beeb6ee67a846f0e530998c19bbc`; no second installation was created.
- Windows, isolated Host profile and test directories, software-rendered Electron.
  Pi used the configured `openai-codex/gpt-6-luna` subscription. Runtime grounding
  and decision calls used the configured external Luna API; those are a separate,
  potentially billable connection. No provider or input gate was bypassed.
- The previously completed 2026-10-29 distribution/static and HTTP-owner lifecycle
  review remains supporting evidence at its original scope. This run does not
  reclassify those checks as GUI evidence or qualify arbitrary Runtime versions.

## Observed live results

| Check | Observed result |
| --- | --- |
| Four-step GUI task | Documents → Reports → Up once → Receipts completed in one plan, with four original navigation executions. The original final image shows Receipts and its marker file. |
| Original-image review | The first step deliberately required Main's post-action judgment. Pi retrieved the bound original after image, made one read-only `observe`, reviewed the current evidence packet and continued the original plan once. No input replay or recovery tool was used. |
| Evidence integrity | All 28 file references in the four-step history and all four original after-image digests matched retained bytes. Both fixture marker hashes and the file inventory were unchanged. |
| Operator-owned window | The original 7-Zip window and process remained open after Runtime cleanup, at Receipts. A restricted process lookup initially hid it; native window inspection and an unrestricted process-identity check confirmed preservation. |
| Session cleanup | The session report records `phase=stopped`, `host_phase=stopped`, no cleanup errors and a stopped sampler. Pi's final status reported no pending requests and verified cleanup. |
| Official maintenance settle | The unmodified official updater returned `settled=true`, `registered_sessions=5`, `cleared_sessions=5`. After the remaining diagnostic/install lifecycle, a second check returned 0/0. Coverage is registered sessions only; legacy sessions were not proven. |
| Missing service | The real Plugins → Test connection action displayed the service/URL/port diagnosis. A fresh Pi Work launched through the actual chat UI showed a native `fetch failed` warning with the Plugins / Settings → Providers → MCP and Test connection directions. |
| Wrong token | The real Plugins connection test displayed HTTP 401 and a token configuration hint. The earlier independent Pi attempt exposed native authentication-required failure; the final warning wording and credential redaction also passed regression tests. |
| Disable while service runs | Native disable removed the projected connection after Host restart; the separately running Runtime retained the same process identity. |
| Uninstall/reinstall | After the Host/backend stopped, the real installer removed the exact connector with `deleteData=false`, then installed/enabled the same wheel again using native secret storage. The independent Runtime remained running across removal. |

The first verification used a supplied, valid four-step v3 plan and a deliberate
manual post-action wait. The other three fixture conditions used the documented
decision-profile configuration. This proves the review/observe/continue contract;
it is not a sample of spontaneous double-rejection frequency or general GUI accuracy.

Earlier unsuccessful attempts are retained: screen occlusion, a Computer Use cursor
overlay, interrupted test processes, and an incorrect test-plan review setting.
The latter was corrected in the test input, not Runtime code. The successful run
is not presented as an error-free first attempt. Per-tool approvals were resolved
through the Host's normal permission API; status alone received a Work-scoped grant.

The execution host completed its native cleanup. The external HTTP daemon was
then terminated by the test controller, with exit code 1; this is not evidence of
graceful daemon shutdown. The isolated Electron Host later closed normally with
exit code 0. Test processes were stopped; the operator's 7-Zip window was retained.

## Host repairs and validation

The installed-plugin card now tests its actual Host-issued MCP connection ID.
The existing public connection projection includes its package owner so the UI
does not reconstruct IDs. HTTP authentication failures retain their transport
status through the MCP client; 401, 403 and unreachable-service diagnoses give
actionable, credential-free guidance. Other statuses retain native MCP negotiation.
Pending configuration cannot probe an obsolete connection. A cancelled settings
refresh cannot leave a stale reconnecting error after a successful restart.

- `python -m pytest -q tests/test_mcp_connections.py tests/test_extension_mcp.py tests/test_pi_provider.py`:
  **45 passed**. Includes native HTTP negotiation, authentication/unreachable
  diagnostics, projection isolation and native Pi notification redaction.
- `npm --prefix electron run build`: passed.
- `electron tests/extensionSettings.smoke.mjs --disable-gpu`: **14 checks passed**,
  including connection results, unapplied settings and stale-refresh recovery.
- Ruff on the five changed Python files and `git diff --check`: passed.
- Full repository CI remains a requirement for merging the Host changes; this
  record does not substitute the targeted checks for that CI.

Raw evidence remains local because it includes screenshots, process metadata and
test-profile details. The ignored audit directory
`.amadeus-test/gui-runtime-008-live-20261105/` retains `final-gui-evidence.json`,
`official-settle-final.json`, `official-settle-post-lifecycle.json`, the original
Runtime receipts/images, UI text observations and native installer receipts.
The GUI evidence collector verifies hashes without executing the package.

## Remaining publication gates

No new connector byte defect was established by this run; these results do not
justify requesting another connector version. The next owner is the maintainer:

1. Integrate the tested Host diagnostic fixes and complete their required CI.
2. Complete the full curated-v1 artifact/ownership/rights review record, then add
   the exact 0.0.8 `catalog/packages/` input to the same submission PR.
3. Obtain eligible non-author approval on the final head before merging inputs.
4. Resolve the repository-governance and release gates, then use a separate index
   publication PR and verify the official served bytes.

Candidate-only admission still does not establish approval. This execution added
no catalog approval, changed no repository protections and published no index.

## 中文结论

0.0.8 + 0.1.11 的四步 GUI、原图审核、observe/continue、窗口保留、文件不变、
收尾和官方 settle 已通过独立实机验收；禁用、卸载和重装也确认了外部 Runtime
由用户独立管理的边界。连接诊断在带本轮修复的 Host 上通过，不能提前称为已发布功能。
不需要因这轮结果要求作者再发一个版本。正式收录、最终非作者批准、治理检查和独立
发布 PR 仍须完成，实机通过不等于已经上架。
