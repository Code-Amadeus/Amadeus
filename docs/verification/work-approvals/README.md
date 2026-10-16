# Work approval choices

Related issue: [#224](https://github.com/Code-Amadeus/Amadeus/issues/224).

This change adds two explicit choices while keeping automatic approval off by
default: a persistent all-Work execution policy, and a revocable exact-call Pi
MCP grant confined to the active Provider run. Desktop settings own persistence;
Provider Runtime owns policy application and audit, and Pi owns its native
callback translation. Restoring a native session never restores a grant.

## Deterministic checks

- Real Pi 1.2.0 subprocesses, a loopback synthetic model and local MCP peers:
  repeated calls, changed arguments/tools/connections, revocation, cancellation,
  native session restoration and connection changes.
- Host contracts: request publication before automatic response, immediate
  grant-use/expiry audit, immutable user decisions, unknown responses not
  resent, concurrent Work isolation, hot enable/disable and partial failures.
- Presentation contracts: ordinary Work/Cooperative approval and revocation,
  multiple grants, foreground ownership, background expiry, persistent settings
  reopening and accurate feedback after partial application.
- Electron `npm test`: 529 passed. `npm run build` and Pi typecheck passed.
- Final approval/Provider/Work/Cooperative/Canvas/wallpaper regression selection:
  77 passed, including real Pi calls across two automatically approved Work runs
  followed by a third run that waits for approval after the switch is disabled.
- The complete isolated Python suite ran in four shards on the documented
  CPython 3.12.10 baseline: 5,934 passed, 34 skipped and 4 failed initially.
  The new scrollbar declaration violated the existing shared-surface contract;
  it was removed, and the wallpaper/Canvas checks then passed (16 checks).
  An unchanged texture-cache HTTP test hit a Windows connection-aborted error;
  its complete module passed on rerun (25 checks).
- Two unchanged optional Codex Skill tests remain blocked before model/tool
  execution: their fixture sets `windows.sandbox = 'mxc'`, which both the local
  CLI 0.130.0 and locked CLI 0.154.0 reject (accepted values are `elevated` or
  `unelevated`). That independent sandbox fixture is unchanged. These local
  failures do not establish an approval-policy failure; the original failures
  and reruns are reported separately from the hosted CI checks.

An initial CPython 3.12.14 run exposed existing Windows character-test fixtures
that clear `SystemRoot` before importing aiohttp/OpenSSL. Those source/test
files were unchanged. Their 20 checks passed on CPython 3.12.10 without changing
fixtures. No unrelated environment workaround is included in this change.

## Hosted CI / 远端 CI (2026-10-17)

All six hosted workflows succeeded for the frozen code candidate
`2dbb5dcef6f1a664679417d526430ad7c2871e89`:

- [Python Windows](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909693805):
  all three full-suite shards, source/dependency checks, source and packaged
  Electron smoke, the environment ladder and the `cpu-model-less` gate passed.
- [Electron Windows](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909693523):
  renderer tests/build, native UI checks, packaging and packaged wallpaper smoke passed.
- [Python Linux](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909692876)
  and [Python macOS](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909692749) passed.
- [Optional character RAG](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909693771)
  and [local-model installation candidates](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909693628) passed.

The follow-up verification-note commit changes this Markdown file only;
executable code and tests remain identical to that tested candidate. The local
optional Codex limitations above remain recorded independently of hosted results.

中文：冻结代码的六组远端 CI 全部通过，包括 Windows 三个完整测试分片、安装包启动
和汇总门禁。后续结项提交只补本文档，不修改已验收的代码或测试。

## Rendered evidence

These screenshots render the real Canvas source with synthetic Work identities
in an isolated headless browser. The before view uses the base commit; the after
views use the changed source. No live user Work, model credentials or real GUI
operation is involved. The active-grant check also verifies that the center of
the Revoke button is not covered by another element.

| Before | Exact-call choice | Active grant and persistent-policy status |
| --- | --- | --- |
| ![Per-call approval](before.png) | ![Exact-call approval](after.png) | ![Visible revocation and policy](active.png) |

中文：默认关闭全自动；可选择跨重启保存的所有 Work 自动审批，或仅本次运行的确切
Pi MCP 调用授权。验证使用本地合成模型、MCP 和隔离浏览器，未替换日常安装版或操作用户应用。
