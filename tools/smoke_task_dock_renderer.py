from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = ROOT / "render" / "web" / "crt_canvas_surface.js"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(
            r"""
const window = globalThis;
const requests = [];
const responseQueue = [];
globalThis.HTMLElement = function HTMLElement() {};
window.location = { origin: "http://127.0.0.1:17777", search: "" };
window.__amadeusBridgePort = "17797";
window.__amadeusBridgeToken = "test-token";
window.setInterval = function () {};
window.setTimeout = function () {};
window.clearTimeout = function () {};
window.localStorage = { getItem() { return null; }, setItem() {} };
function queueJsonResponse(status, payload) {
  responseQueue.push({
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
  });
}
window.fetch = async function (url, options) {
  requests.push({ url, options: options || {} });
  if (responseQueue.length) return responseQueue.shift();
  return { ok: true, status: 200, async json() { return { ok: true }; } };
};
window.document = {
  getElementById() { return null; },
  createElement() { return element(); },
  head: { appendChild() {} },
  body: { appendChild() {} },
};
globalThis.navigator = {};
function element() {
  return {
    innerHTML: "",
    textContent: "",
    id: "",
    className: "",
    style: { setProperty() {} },
    classList: { remove() {}, add() {}, contains() { return false; }, toggle() {} },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    appendChild() {},
  };
}
"""
        )
        tmp.write(source.read_text(encoding="utf-8"))
        tmp.write(
            r"""
function assert(condition, message) {
  if (!condition) throw new Error(message);
}

(async function () {
  const surface = window.createCrtCanvasSurface();
  const taskDock = {
    revision: "ledger-7",
    selectedWorkItemId: "work-2",
    workspaceFocusMode: "pinned",
    workspaceFocusWorkItemId: "work-2",
    workspaceFocusPath: "C:\\workspace\\slice-dock",
    counts: { running: 2, needsAttention: 1, active: 4 },
    items: [
      { id: "work-1", title: "Running sibling", execution: "running", liveness: "stalled", livenessStage: "events", probeStatus: "running", silentForSeconds: 305, completion: "incomplete", attention: "none", workspaceLabel: "codex/task-one", updatedAt: "2026-07-12T09:00:00Z" },
      { id: "work-2", attemptId: "attempt-3", title: "Selected Slice task", state: "review_ready", execution: "succeeded", completion: "partial", attention: "review", workspaceLabel: "codex/slice-dock", workspacePath: "C:\\workspace\\slice-dock", updatedAt: "2026-07-12T09:01:00Z" },
      { id: "work-3", title: "Permission needed", execution: "succeeded", completion: "partial", attention: "permission", workspaceLabel: "codex/export", updatedAt: "2026-07-12T09:02:00Z" },
      { id: "work-4", title: "Accepted change", execution: "succeeded", completion: "accepted", attention: "none", state: "accepted", workspaceLabel: "main", updatedAt: "2026-07-12T09:03:00Z" },
      { id: "work-5", title: "Recent audit", state: "open", execution: "succeeded", completion: "complete", attention: "none", workspaceLabel: "codex/audit", updatedAt: "2026-07-12T09:04:00Z" },
      { id: "work-6", title: "Fifth current task", execution: "running", completion: "unknown", attention: "none", workspaceLabel: "codex/six", updatedAt: "2026-07-12T09:05:00Z" },
      { id: "work-7", title: "Seventh current task", execution: "running", completion: "unknown", attention: "none", workspaceLabel: "codex/seven", updatedAt: "2026-07-12T09:06:00Z" },
      { id: "work-8", title: "History eight", execution: "succeeded", completion: "accepted", attention: "none", state: "accepted", workspaceLabel: "codex/history-eight", updatedAt: "2026-07-12T09:07:00Z" },
      { id: "work-9", title: "History nine", execution: "succeeded", completion: "accepted", attention: "none", state: "archived", workspaceLabel: "codex/history-nine", updatedAt: "2026-07-12T09:08:00Z" },
      { id: "work-10", title: "History ten", execution: "succeeded", completion: "accepted", attention: "none", state: "closed", workspaceLabel: "codex/history-ten", updatedAt: "2026-07-12T09:09:00Z" },
      { id: "work-11", title: "History eleven", execution: "succeeded", completion: "accepted", attention: "none", state: "accepted", workspaceLabel: "codex/history-eleven", updatedAt: "2026-07-12T09:10:00Z" },
      { id: "work-12", title: "History twelve", execution: "succeeded", completion: "accepted", attention: "none", state: "archived", workspaceLabel: "codex/history-twelve", updatedAt: "2026-07-12T09:11:00Z" },
      { id: "work-13", title: "History thirteen", execution: "succeeded", completion: "accepted", attention: "none", state: "closed", workspaceLabel: "codex/history-thirteen", updatedAt: "2026-07-12T09:12:00Z" },
      { id: "work-14", title: "History fourteen", execution: "succeeded", completion: "accepted", attention: "none", state: "accepted", workspaceLabel: "codex/history-fourteen", updatedAt: "2026-07-12T09:13:00Z" }
    ]
  };
  const context = { projectId: "project-amadeus", workItemId: "work-2", runId: "run-9", attemptId: "attempt-3" };
  const selectedDiff = {
    additions: 2,
    deletions: 0,
    files: [{
      path: "slice.js",
      status: "modified",
      additions: 1,
      deletions: 0,
      hunks: [{ header: "@@ -1 +1,2 @@", lines: [{ kind: "add", newLine: 2, text: "task dock" }] }]
    }, {
      path: "slice.css",
      status: "modified",
      additions: 1,
      deletions: 0,
      hunks: [{ header: "@@ -4 +4,2 @@", lines: [{ kind: "add", newLine: 5, text: ".task-dock {}" }] }]
    }]
  };
  const selectedPresentation = {
    mode: "diff",
    phase: "Review",
    title: "Selected task diff",
    lead: "Canonical selected task projection.",
    reportMarkdown: "### Selected task report\nTask-owned report.",
    workContext: context,
    diff: selectedDiff,
    open: true
  };

  surface.setPayload({ ...selectedPresentation, taskDock });
  surface.__testRailExpanded(true);

  let html = surface.__testHtml();
  assert(html.includes("crt-canvas-task-dock"), "task dock was not rendered");
  assert(html.includes("Current 6") && html.includes("Needs you 2"), "task filter counts were not rendered");
  assert(surface.__testDock().counts.running === 2 && surface.__testDock().counts.needsAttention === 1, "task dock counts were not retained");
  assert(html.includes('data-work-item-id="work-2" aria-current="true"'), "selected task is not canonical server selection");
  assert(html.includes("Workspace locked:") && html.includes("C:\\workspace\\slice-dock"), "workspace routing lock was not rendered");
  assert(html.includes('data-work-focus-mode="auto"'), "workspace unlock mode was not rendered");
  assert(!html.includes('data-work-focus-mode="auto" data-work-focus-item-id='), "workspace unlock retained the stale locked WorkItem id");
  assert(html.includes(">Unlock</button>"), "workspace unlock action was not rendered");
  assert(html.includes("Needs you / succeeded"), "attention category was not rendered");
  assert(html.includes("Stalled 5m 5s") && html.includes("Provider reports running"), "provider liveness was not rendered in the task dock");
  assert(!html.includes('data-work-execution-action="continue"'), "completed work exposed the removed Continue action");
  assert(html.includes('data-work-item-id="work-7"'), "Current filter clipped a task supplied by the task dock");
  assert(html.includes("show-report") && html.includes("show-diff"), "task dock broke Report/Diff tabs");

  surface.__testMode("markdown");
  assert(surface.__testHtml().includes('data-action="show-report" class="active"'), "Report tab did not become active");
  surface.setPayload({
    ...selectedPresentation,
    taskDock: { ...taskDock, revision: "ledger-8", counts: { ...taskDock.counts, running: 3 } }
  });
  html = surface.__testHtml();
  assert(html.includes('data-action="show-report" class="active"'), "same presentation taskDock replay discarded the selected Report tab");
  assert(html.includes("Task-owned report."), "same presentation taskDock replay discarded the report body");

  surface.__testMode("diff");
  surface.__testDiffFile(1);
  assert(surface.__testHtml().includes('class="crt-canvas-diff-file active" data-diff-file-index="1"'), "non-first diff file did not become active");
  surface.setPayload({
    ...selectedPresentation,
    taskDock: { ...taskDock, revision: "ledger-9", counts: { ...taskDock.counts, needsAttention: 2 } }
  });
  html = surface.__testHtml();
  assert(html.includes('class="crt-canvas-diff-file active" data-diff-file-index="1"'), "same presentation taskDock replay discarded the active diff file");
  assert(html.includes("slice.css"), "same presentation taskDock replay discarded the selected diff content");

  surface.__testFilter("needs");
  html = surface.__testHtml();
  assert(html.includes('data-work-filter="needs" class="active" aria-pressed="true"'), "Needs you filter did not become active");
  assert(html.includes('data-work-item-id="work-2"') && html.includes('data-work-item-id="work-3"'), "Needs you filter omitted attention tasks");
  assert(!html.includes('data-work-item-id="work-1"') && !html.includes('data-work-item-id="work-4"'), "Needs you filter retained unrelated tasks");

  surface.__testFilter("history");
  html = surface.__testHtml();
  assert(html.includes('data-work-filter="history" class="active" aria-pressed="true"'), "History filter did not become active");
  assert(html.includes('data-work-item-id="work-4"'), "History filter omitted accepted work");
  assert(html.includes('data-work-item-id="work-14"') && html.includes("History fourteen"), "History filter clipped entries after the fifth item");
  assert(html.includes('data-work-item-id="work-2" aria-current="true"'), "server-selected task disappeared outside its filter");
  assert(html.includes("Viewing outside filter"), "selected task outside the filter was not explained");
  assert(!html.includes('data-work-item-id="work-1"') && !html.includes('data-work-item-id="work-3"'), "History filter retained unrelated current work");

  surface.__testFilter("current");
  html = surface.__testHtml();
  assert(html.includes('data-work-filter="current" class="active" aria-pressed="true"'), "Current filter did not become active again");
  assert(html.includes('data-work-item-id="work-1"') && !html.includes('data-work-item-id="work-4"'), "Current filter did not restore active work");

  await surface.__testSelect("work-3");
  await surface.__testFocus("auto", "");
  assert(requests.length === 2, "task dock actions did not reach the canvas action endpoint");
  const selectBody = JSON.parse(requests[0].options.body);
  const focusBody = JSON.parse(requests[1].options.body);
  assert(selectBody.target === "work_item" && selectBody.action === "select", "selection escaped the work_item intent boundary");
  assert(selectBody.work_item_id === "work-3" && selectBody.project_id === "project-amadeus", "selection lost canonical task context");
  assert(selectBody.revision === "ledger-9", "selection omitted the latest ledger revision");
  assert(focusBody.target === "work_item" && focusBody.action === "set_focus" && focusBody.focus_mode === "auto", "focus action contract is wrong");
  assert(focusBody.work_item_id === "", "workspace unlock sent the stale locked WorkItem identity");
  assert(surface.__testDock().selectedWorkItemId === "work-2", "renderer optimistically changed the canonical selection");

  surface.setPayload({
    mode: "workflow",
    phase: "Work",
    title: "Selected task resumed",
    lead: "A complete canonical snapshot replaces the prior selected view.",
    workContext: context,
    taskDock,
    signals: [{ label: "provider", text: "The selected task is working again." }],
    open: true
  });
  html = surface.__testHtml();
  assert(!html.includes("Task-owned report") && !html.includes("show-report") && !html.includes("show-diff"), "complete canonical snapshot merged stale selected-task content");

  surface.setPayload({
    mode: "markdown",
    title: "Attempt three report",
    markdown: "STALE_ATTEMPT_THREE_REPORT",
    workContext: context,
    open: true
  });
  surface.setPayload({
    mode: "workflow",
    title: "Attempt four started",
    workContext: { ...context, runId: "run-10", attemptId: "attempt-4" },
    signals: [{ label: "provider", text: "The next attempt is running." }],
    open: true
  });
  html = surface.__testHtml();
  assert(!html.includes("STALE_ATTEMPT_THREE_REPORT") && !html.includes("show-report") && !html.includes("show-diff"), "report/diff content leaked across attempts of one WorkItem");

  surface.setPayload({
    mode: "workflow",
    phase: "Work",
    title: "Other selected task",
    lead: "The server selected a different task.",
    workContext: { projectId: "project-amadeus", workItemId: "work-3", runId: "run-10", attemptId: "attempt-1" },
    taskDock: { ...taskDock, selectedWorkItemId: "work-3", focusMode: "auto" },
    signals: [{ label: "provider", text: "Working in the other task." }],
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes('data-work-item-id="work-3" aria-current="true"'), "new canonical selection was not rendered");
  assert(!html.includes("Task-owned report") && !html.includes("show-report") && !html.includes("show-diff"), "report/diff content leaked across work items");

  surface.setPayload({
    mode: "diff",
    title: "Pending desktop export",
    reason: "Desktop export is waiting for explicit approval.",
    pending_export: true,
    diff: { files: [] },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes("External export pending"), "empty diff did not distinguish an external export");
  assert(html.includes("Desktop export is waiting for explicit approval."), "empty diff discarded the payload reason");

  surface.setPayload({
    mode: "diff",
    title: "Historical diff",
    reason: "attempt_diff_unavailable",
    diff_available: false,
    diff: { files: [] },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes("Attempt diff unavailable"), "empty diff did not distinguish an unavailable attempt baseline");
  assert(html.includes("historical diff cannot be reconstructed"), "unavailable diff did not explain the missing baseline");

  surface.setPayload({
    mode: "diff",
    title: "Missing staged export",
    diff: {
      files: [],
      available: false,
      reasonCode: "staged_export_missing",
      reason: "Codex did not create the requested staged deliverable."
    },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes("Staged deliverable missing"), "missing staged export was reduced to a generic unavailable diff");
  assert(!html.includes("Attempt diff unavailable"), "missing staged export was misclassified as historical diff loss");

  surface.setPayload({
    mode: "diff",
    title: "Unsafe staged export",
    diff: {
      files: [],
      available: false,
      reasonCode: "export_discovery_error",
      reason: "Amadeus could not safely inspect the staged deliverable."
    },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes("Export inspection failed"), "export discovery failure was reduced to a generic unavailable diff");
  assert(!html.includes("Attempt diff unavailable"), "export discovery failure was misclassified as historical diff loss");

  surface.setPayload({
    mode: "diff",
    title: "Blocked diff",
    blocked: true,
    blocked_reason: "Provider approval is required before workspace changes can start.",
    diff: { files: [] },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes("Diff blocked"), "empty diff did not distinguish a blocked attempt");
  assert(html.includes("Provider approval is required before workspace changes can start."), "blocked diff discarded its reason");

  surface.setPayload({
    mode: "diff",
    title: "Clean workspace",
    diff: { files: [], clean: true },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes("No workspace changes"), "clean attempt used a generic empty diff state");
  assert(html.includes("did not change files inside the workspace"), "clean attempt did not explain the workspace boundary");

  surface.setPayload({
    mode: "permission",
    phase: "Checkpoint",
    title: "Approval required",
    workContext: context,
    taskDock,
    permissionVisible: true,
    permissionRequest: {
      id: "permission-export-chess",
      workItemId: "work-2",
      attemptId: "attempt-permission-current",
      capability: "filesystem.export",
      action: "copy_to_desktop",
      scope: [
        "C:\\Users\\Example\\Desktop\\chess_game.py",
        "C:\\Users\\Example\\Desktop\\README.md",
        "C:\\Users\\Example\\Desktop\\piece.py",
        "C:\\Users\\Example\\Desktop\\board.py",
        "C:\\Users\\Example\\Desktop\\theme.json",
        "C:\\Users\\Example\\Desktop\\LICENSE.txt",
        "C:\\Users\\Example\\Desktop\\seventh-target.txt"
      ],
      reason: "Export the validated chess game to Desktop.",
      reversibility: "Creates a new file and never overwrites an existing file.",
      options: ["allow_once", "deny"]
    },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes("filesystem.export / copy_to_desktop"), "permission card kept the static demo operation");
  assert(html.includes("chess_game.py") && html.includes("Export the validated chess game"), "permission card omitted exact scope or reason");
  assert(html.includes("seventh-target.txt"), "permission card clipped exact scope targets after the sixth item");
  assert(html.includes("Allow once covers all 7 listed targets."), "permission card did not explain the complete allow-once authority");
  assert(html.includes('data-permission-action="allow_once"') && html.includes('data-permission-action="deny"'), "permission choices were not rendered from the request");
  await surface.__testPermission("allow_once");
  assert(requests.length === 3, "permission action did not reach the canvas bridge");
  const permissionBody = JSON.parse(requests[2].options.body);
  assert(permissionBody.target === "permission" && permissionBody.action === "allow_once", "permission action escaped its target boundary");
  assert(permissionBody.permission_request_id === "permission-export-chess", "permission action lost the durable request id");
  assert(permissionBody.work_item_id === "work-2" && permissionBody.revision === "ledger-7", "permission action lost canonical WorkItem context");
  assert(permissionBody.attempt_id === "attempt-permission-current", "permission action used stale presentation attempt instead of request identity");
  assert(!Object.prototype.hasOwnProperty.call(permissionBody, "path"), "renderer sent an authority-bearing path instead of the ledger request id");
  assert(!surface.__testHtml().includes("filesystem.export"), "resolved permission card remained visible");

  surface.setPayload({
    mode: "permission",
    title: "Unknown permission scope",
    workContext: context,
    taskDock,
    permissionVisible: true,
    permissionRequest: {
      id: "permission-without-scope",
      capability: "provider.tool",
      action: "run",
      scope: [],
      reason: "The provider omitted its path scope.",
      options: ["allow_once", "deny"]
    },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes("did not report exact path targets"), "empty permission scope did not disclose the missing target boundary");
  assert(!html.includes("Scope: this WorkItem only"), "empty permission scope invented a WorkItem-only authority boundary");

  surface.setPayload({
    mode: "permission",
    title: "Deny-only provider permission",
    workContext: context,
    taskDock,
    permissionVisible: true,
    permissionRequest: {
      id: "permission-deny-only",
      capability: "provider.tool",
      action: "diagnostic_retry",
      scope: [],
      reason: "This retrospective provider request cannot be approved in-place.",
      options: ["deny"],
      diagnosticOnly: true,
      retryRequired: true
    },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes('data-permission-action="deny"'), "deny-only permission did not render its safe action");
  assert(!html.includes('data-permission-action="allow_once"'), "deny-only permission invented Allow once");
  assert(html.includes(">Dismiss</button>") && html.includes("cannot be approved in place"), "provider diagnostic still looked like an approvable permission");
  assert(!html.includes("Approval requires a provider retry"), "provider diagnostic retained misleading approval copy");

  surface.setPayload({
    mode: "permission",
    title: "Empty permission options",
    workContext: context,
    taskDock,
    permissionVisible: true,
    permissionRequest: {
      id: "permission-empty-options",
      capability: "provider.tool",
      action: "unknown",
      scope: [],
      reason: "No executable approval option was supplied.",
      options: []
    },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes('data-permission-action="deny"'), "empty permission options did not fail closed to Deny");
  assert(!html.includes('data-permission-action="allow_once"'), "empty permission options invented Allow once");
  const requestCountBeforeRejectedAllow = requests.length;
  await surface.__testPermission("allow_once");
  assert(requests.length === requestCountBeforeRejectedAllow, "renderer forwarded an action absent from immutable permission options");

  surface.setPayload({
    mode: "permission",
    title: "Export recovery required",
    workContext: context,
    taskDock,
    permissionVisible: true,
    permissionRequest: {
      id: "permission-export-recovery",
      workItemId: "work-2",
      attemptId: "attempt-recovery-current",
      capability: "filesystem.export",
      action: "resume_authorized_export",
      scope: ["C:\\Users\\Example\\Desktop\\chess_game.py"],
      reason: "Retry only the already-approved target.",
      status: "allowed",
      options: ["retry_export", "abandon_export"]
    },
    open: true
  });
  html = surface.__testHtml();
  assert(html.includes("Recovery") && html.includes('data-permission-action="retry_export"'), "authorized export recovery action was not rendered");
  assert(html.includes('data-permission-action="abandon_export"') && html.includes("Abandon export"), "authorized export abandon action was not rendered");
  assert(!html.includes('data-permission-action="allow_once"'), "recovery card incorrectly requested a second allow-once decision");
  await surface.__testPermission("retry_export");
  assert(requests.length === 4, "recovery action did not reach the canvas bridge");
  const recoveryBody = JSON.parse(requests[3].options.body);
  assert(recoveryBody.action === "retry_export" && recoveryBody.permission_request_id === "permission-export-recovery", "recovery action lost its immutable ledger identity");
  assert(recoveryBody.attempt_id === "attempt-recovery-current", "recovery action used stale presentation attempt instead of request identity");
  surface.setPayload({
    mode: "permission",
    title: "Export recovery required",
    workContext: context,
    taskDock,
    permissionVisible: true,
    permissionRequest: {
      id: "permission-export-recovery",
      workItemId: "work-2",
      attemptId: "attempt-recovery-current",
      capability: "filesystem.export",
      action: "resume_authorized_export",
      scope: ["C:\\Users\\Example\\Desktop\\chess_game.py"],
      reason: "Retry only the already-approved target.",
      status: "allowed",
      options: ["retry_export", "abandon_export"]
    },
    open: true
  });
  await surface.__testPermission("abandon_export");
  assert(requests.length === 5, "abandon export action did not reach the canvas bridge");
  const abandonBody = JSON.parse(requests[4].options.body);
  assert(abandonBody.action === "abandon_export" && abandonBody.permission_request_id === "permission-export-recovery", "abandon export action lost its immutable ledger identity");
  assert(abandonBody.attempt_id === "attempt-recovery-current", "abandon export action used stale presentation attempt instead of request identity");
  surface.setPayload({
    ...selectedPresentation,
    taskDock: { ...taskDock, revision: "ledger-10" }
  });
  surface.__testRailExpanded(false);
  html = surface.__testHtml();
  assert(html.includes('data-work-disposition-toggle="work-2"'), "collapsed task strip omitted its disposition dot");
  surface.__testRailExpanded(true);
  surface.__testDispositionToggle("work-5");
  html = surface.__testHtml();
  assert(html.includes('data-work-disposition-action="archive"') && !html.includes('data-work-disposition-action="accept"'), "open WorkItem disposition was not Archive-only");
  surface.__testDispositionToggle("work-2");
  html = surface.__testHtml();
  assert(html.includes('data-work-disposition-action="accept"') && html.includes('data-work-disposition-action="archive"'), "review-ready WorkItem omitted Accept or Archive");
  const dispositionRequestStart = requests.length;
  await surface.__testDisposition("accept", "work-2");
  assert(requests.length === dispositionRequestStart + 1, "task disposition did not reach the canvas bridge");
  const dispositionBody = JSON.parse(requests[dispositionRequestStart].options.body);
  assert(dispositionBody.target === "work_item" && dispositionBody.action === "accept", "task disposition escaped the work_item intent boundary");
  assert(dispositionBody.work_item_id === "work-2" && dispositionBody.revision === "ledger-10", "task disposition lost WorkItem identity or revision");
  const requestCountBeforeRemovedContinue = requests.length;
  await surface.__testExecution("continue", "work-2", "attempt-3");
  assert(requests.length === requestCountBeforeRemovedContinue, "removed Continue action still reached the canvas bridge");

  const retryDock = {
    ...taskDock,
    revision: "ledger-11",
    items: taskDock.items.map((item) => item.id === "work-2"
      ? { ...item, execution: "failed", attention: "error", canRetry: true }
      : item)
  };
  surface.setPayload({ ...selectedPresentation, taskDock: retryDock });
  html = surface.__testHtml();
  assert(html.includes('data-work-execution-action="retry"') && html.includes(">Retry</button>"), "retry action was not rendered for a failed attempt");
  await surface.__testExecution("retry", "work-2", "attempt-3");
  assert(requests.length === requestCountBeforeRemovedContinue + 1, "retry action did not reach the canvas bridge");
  const retryBody = JSON.parse(requests[requestCountBeforeRemovedContinue].options.body);
  assert(retryBody.target === "work_item" && retryBody.action === "retry", "retry escaped the work_item intent boundary");
  assert(retryBody.work_item_id === "work-2" && retryBody.attempt_id === "attempt-3" && retryBody.revision === "ledger-11", "retry lost canonical task identity or revision");

  const authorizationDock = {
    ...retryDock,
    items: retryDock.items.map((item) => item.id === "work-2"
      ? { ...item, retryAuthorizationRequestId: "permission-provider-denied" }
      : item)
  };
  surface.setPayload({ ...selectedPresentation, taskDock: authorizationDock });
  html = surface.__testHtml();
  assert(html.includes(">Authorize &amp; Retry</button>"), "denied provider request did not require an explicit authorization click");
  await surface.__testExecution("retry", "work-2", "attempt-3", "permission-provider-denied");
  const authorizationBody = JSON.parse(requests[requests.length - 1].options.body);
  assert(authorizationBody.authorization_permission_request_id === "permission-provider-denied", "authorized retry lost its immutable permission request identity");
  assert(!Object.prototype.hasOwnProperty.call(authorizationBody, "capability") && !Object.prototype.hasOwnProperty.call(authorizationBody, "scope"), "authorized retry leaked renderer-authored authority fields");

  const failedActionRequestStart = requests.length;
  queueJsonResponse(400, {
    ok: false,
    error: "stale_revision",
    work: { ...retryDock, revision: "ledger-12" },
  });
  await surface.__testSelect("work-3");
  html = surface.__testHtml();
  assert(surface.__testDock().revision === "ledger-12", "HTTP 400 selection discarded the latest work projection");
  assert(html.includes('role="alert"') && html.includes("Task state changed"), "selection failure remained invisible");

  queueJsonResponse(400, {
    ok: false,
    error: "workspace_lock_conflict",
    projection: { ...retryDock, revision: "ledger-13", workspaceFocusMode: "auto", workspaceFocusPath: "" },
  });
  await surface.__testFocus("pinned", "work-2");
  html = surface.__testHtml();
  assert(surface.__testDock().revision === "ledger-13", "HTTP 400 workspace lock discarded the compatibility projection");
  assert(html.includes("Unable to update workspace routing") && html.includes("workspace lock conflict"), "workspace lock failure remained invisible");

  queueJsonResponse(400, {
    ok: false,
    error: "work_action_not_available",
    work: { ...retryDock, revision: "ledger-14" },
  });
  await surface.__testExecution("retry", "work-2", "attempt-3");
  html = surface.__testHtml();
  assert(surface.__testDock().revision === "ledger-14", "HTTP 400 retry discarded the latest work projection");
  assert(html.includes("task action is no longer available"), "retry failure remained invisible");

  queueJsonResponse(400, {
    ok: false,
    error: "work_attempt_not_current",
    work: { ...retryDock, revision: "ledger-15" },
  });
  await surface.__testExecution("resume", "work-2", "attempt-3");
  html = surface.__testHtml();
  assert(surface.__testDock().revision === "ledger-15", "HTTP 400 resume discarded the latest work projection");
  assert(html.includes("run is no longer the current attempt"), "resume failure remained invisible");

  const failedActionBodies = requests.slice(failedActionRequestStart).map((request) => JSON.parse(request.options.body));
  assert(failedActionBodies.length === 4, "one or more failed task actions never reached the canvas bridge");
  assert(failedActionBodies.map((body) => body.action).join(",") === "select,set_focus,retry,resume", "failed task action coverage did not exercise Select/Pin/Retry/Resume");
  assert(failedActionBodies.map((body) => body.revision).join(",") === "ledger-11,ledger-12,ledger-13,ledger-14", "refreshed work projections were not used by the next task action");

  const destinationDock = {
    ...taskDock,
    revision: "ledger-16",
    workspaceFocusMode: "auto",
    workspaceFocusWorkItemId: "",
    workspaceFocusPath: "",
    destinationLabel: "amadeus",
  };
  surface.setPayload({ ...selectedPresentation, taskDock: destinationDock });
  html = surface.__testHtml();
  assert(html.includes('data-work-destination-action="exit-project"'), "active project did not expose the Drafts recovery action");
  const exitRequestStart = requests.length;
  await surface.__testDestinationExit();
  const exitBody = JSON.parse(requests[exitRequestStart].options.body);
  assert(exitBody.target === "work_destination" && exitBody.action === "exit_project", "destination exit escaped its bounded intent boundary");
  assert(exitBody.revision === "ledger-16", "destination exit omitted the projected revision");

  surface.setPayload({
    ...selectedPresentation,
    taskDock: {
      ...destinationDock,
      revision: "ledger-17",
      destinationFeedback: { status: "rejected", message: "That project folder is no longer available." },
    },
  });
  html = surface.__testHtml();
  assert(html.includes("That project folder is no longer available."), "rejected project switch was not visible in the task dock");
  console.log("task dock renderer smoke ok");
})().catch((error) => {
  console.error(error && (error.stack || error));
  process.exitCode = 1;
});
"""
        )

    try:
        text = tmp_path.read_text(encoding="utf-8")
        marker = "      layout(bounds) {\n"
        hooks = (
            "      __testHtml() { return card.innerHTML; },\n"
            "      __testStatus() { return status.textContent || status.innerHTML; },\n"
            "      __testDock() { return state.taskDock; },\n"
            "      __testSelect(id) { return handleWorkItemSelect({ getAttribute(name) { return name === 'data-work-item-id' ? id : ''; }, classList: { remove() {}, add() {} } }); },\n"
            "      __testFocus(mode, id) { return handleWorkItemFocus({ getAttribute(name) { if (name === 'data-work-focus-mode') return mode; if (name === 'data-work-focus-item-id') return id; return ''; }, classList: { remove() {}, add() {} } }); },\n"
            "      __testDestinationExit() { return handleDestinationExit({ classList: { remove() {}, add() {} } }); },\n"
            "      __testExecution(action, id, attemptId, authorizationId) { return handleWorkItemExecution({ getAttribute(name) { if (name === 'data-work-execution-action') return action; if (name === 'data-work-action-item-id') return id; if (name === 'data-work-action-attempt-id') return attemptId; if (name === 'data-work-authorization-request-id') return authorizationId || ''; return ''; }, classList: { remove() {}, add() {} } }); },\n"
            "      __testDispositionToggle(id) { state.workDispositionMenu = state.workDispositionMenu === id ? '' : id; render(); },\n"
            "      __testDisposition(action, id) { return handleWorkItemDisposition({ disabled: false, getAttribute(name) { if (name === 'data-work-disposition-action') return action; if (name === 'data-work-disposition-item-id') return id; return ''; }, classList: { remove() {}, add() {} } }); },\n"
            "      __testRailExpanded(value) { state.taskRailExpanded = value === true; render(); },\n"
            "      __testFilter(filter) { return handleTaskFilter({ getAttribute(name) { return name === 'data-work-filter' ? filter : ''; } }); },\n"
            "      __testMode(mode) { return switchCanvasMode(mode); },\n"
            "      __testDiffFile(index) { state.activeDiffFile = Number(index) || 0; render(); },\n"
            "      __testPermission(action) { return handlePermissionAction({ getAttribute(name) { return name === 'data-permission-action' ? action : ''; }, classList: { remove() {}, add() {} } }); },\n"
        )
        if marker not in text:
            raise AssertionError("canvas test hook insertion point not found")
        tmp_path.write_text(text.replace(marker, hooks + marker, 1), encoding="utf-8")
        result = subprocess.run(
            ["node", str(tmp_path)],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)
        print(result.stdout.strip())
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
