from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_main_chat_work_activity_has_no_permission_decision_mount() -> None:
    source = (ROOT / "electron" / "src" / "renderer" / "components"
        / "ChatPage.tsx").read_text(encoding="utf-8")
    assert "onPermissionDecision=" not in source
    assert "chat.permission.resolve" not in source


def test_canvas_header_preview_button_sends_only_work_identity() -> None:
    source = (ROOT / "render" / "web" / "crt_canvas_surface.js").read_text(
        encoding="utf-8"
    )
    marker = "      layout(bounds) {\n"
    hook = (
        "      __testHtml() { return card.innerHTML; },\n"
        "      __testStatusText() { return taskDockCountText(); },\n"
        "      __testTaskMatches(item, filter) { return taskMatchesFilter(item, filter, state.taskDock); },\n"
        "      __testPreview(id, attemptId) { return handleWorkItemPreview({ "
        "getAttribute(name) { if (name === 'data-work-preview-item-id') return id; "
        "if (name === 'data-work-preview-attempt-id') return attemptId; return ''; }, "
        "classList: { remove() {}, add() {} } }); },\n"
        "      __testPermission(action, relativePath = '') { return handlePermissionAction({ "
        "getAttribute(name) { return name === 'data-permission-action' ? action : "
        "name === 'data-export-relative-path' ? relativePath : ''; } }); },\n"
    )
    assert marker in source
    source = source.replace(marker, hook + marker, 1)

    prefix = r"""
const window = globalThis;
class Element {}
class HTMLElement extends Element {}
globalThis.Element = Element;
globalThis.HTMLElement = HTMLElement;
const requests = [];
window.location = { origin: "http://127.0.0.1:17777", search: "" };
window.__amadeusBridgePort = "17797";
window.__amadeusBridgeToken = "test-token";
window.setInterval = function () {};
window.setTimeout = function () {};
window.clearTimeout = function () {};
window.localStorage = { getItem() { return null; }, setItem() {} };
window.fetch = async function (url, options) {
  requests.push({ url, options: options || {} });
  return { ok: true, status: 200, async json() { return { ok: true }; } };
};
window.document = {
  getElementById() { return null; },
  createElement() { return element(); },
  addEventListener() {},
  head: { appendChild() {} },
  body: { appendChild() {}, removeChild() {} },
};
globalThis.navigator = {};
function element() {
  const value = new HTMLElement();
  Object.assign(value, {
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
  });
  return value;
}
"""
    suffix = r"""
function assert(condition, message) {
  if (!condition) throw new Error(message);
}

(async function () {
  const surface = window.createCrtCanvasSurface();
  const taskDock = {
    revision: "ledger-preview",
    currentSessionId: "session-current",
    selectedWorkItemId: "work-preview",
    workspaceFocusMode: "auto",
    counts: { running: 1, needsAttention: 131, active: 1 },
    items: [{
      id: "work-preview",
      sessionId: "session-current",
      attemptId: "attempt-preview",
      title: "Build a web application",
      execution: "running",
      completion: "incomplete",
      attention: "none",
      workspacePath: "C:\\workspace\\web-application",
    }, {
      id: "work-history",
      sessionId: "session-older",
      title: "Historical task with stale attention",
      state: "review_ready",
      execution: "done",
      completion: "complete",
      attention: "review",
    }],
  };
  surface.setPayload({
    open: true,
    mode: "workflow",
    workContext: { workItemId: "work-preview", attemptId: "attempt-preview" },
    taskDock,
  });
  let html = surface.__testHtml();
  const statusText = surface.__testStatusText();
  assert(statusText === "Destination · Drafts · 1 running", "Status bar retained history totals or stale action counts: " + statusText);
  assert(!statusText.includes("tasks"), "Status bar still duplicates the History task total");
  assert(html.includes('data-work-filter="current"'), "Current task filter missing");
  assert(html.includes('data-work-filter="projects"'), "Projects task filter missing");
  assert(html.includes('data-work-filter="history"'), "History task filter missing");
  assert(!html.includes('data-work-filter="needs"'), "Needs-you remains a duplicate task filter");
  assert(
    surface.__testTaskMatches({ sessionId: "session-current", state: "accepted" }, "current"),
    "Current still depends on manual Accept/Archive state"
  );
  assert(
    surface.__testTaskMatches({ sessionId: "session-older", state: "open" }, "history"),
    "History does not follow conversation identity"
  );
  assert(html.includes('class="crt-canvas-preview-launch"'), "Canvas W preview entry missing");
  assert(html.includes('data-work-preview-item-id="work-preview"'), "Canvas W button lost WorkItem identity");
  assert(html.includes('data-work-preview-attempt-id="attempt-preview"'), "Preview attempt identity missing");
  assert(html.includes(">W</button>"), "Canvas W entry was not reused for Preview");
  assert(!html.includes(">Preview</button>"), "Task controls still contain a second Preview entry");
  assert(
    html.split('data-work-preview-item-id=').length - 1 === 1,
    "Preview has more than one visible entry"
  );

  await surface.__testPreview("work-preview", "attempt-preview");
  assert(requests.length === 1, "Preview did not reach the canvas bridge exactly once");
  const body = JSON.parse(requests[0].options.body);
  assert(body.target === "work_item" && body.action === "open_preview", "Preview escaped its WorkItem boundary");
  assert(body.work_item_id === "work-preview", "Preview lost WorkItem identity");
  assert(body.attempt_id === "attempt-preview", "Preview lost Attempt identity");
  assert(body.revision === "ledger-preview", "Preview lost ledger revision");
  assert(
    Object.keys(body).sort().join(",") === "action,attempt_id,revision,target,work_item_id",
    "Preview leaked renderer-authored path, URL, port, or command authority"
  );

  surface.setPayload({
    open: true,
    taskDock: {
      ...taskDock,
      revision: "ledger-waiting",
      items: [{
        id: "work-preview",
        title: "Workspace pending",
        execution: "queued",
        completion: "unknown",
        attention: "none",
      }],
    },
  });
  html = surface.__testHtml();
  const start = html.indexOf('data-work-preview-item-id="work-preview"');
  const end = html.indexOf("</button>", start);
  const button = start >= 0 && end > start ? html.slice(start, end) : "";
  assert(button.includes("disabled"), "workspace-pending Preview was actionable");
  assert(button.includes("workspace and an active attempt"), "workspace-pending Preview lacked bounded waiting feedback");

  const binarySha = "a".repeat(64);
  surface.setPayload({
    open: true,
    permissionVisible: true,
    permissionRequest: {
      id: "permission-binary",
      workItemId: "work-preview",
      attemptId: "attempt-preview",
      capability: "filesystem.export",
      action: "copy_to_desktop",
      scope: ["C:\\Users\\<user>\\Desktop\\portrait.png"],
      reason: "Export one binary image by exact identity.",
      reversibility: "Creates only the listed target.",
      options: ["allow_once", "deny"],
      previewComplete: true,
      previewVersion: 2,
      previews: [{
        path: "Desktop/portrait.png",
        status: "binary_identity",
        mediaType: "image/png",
        sizeBytes: 2048,
        sha256: binarySha,
        source_path: "C:\\private\\workspace\\portrait.png",
        temporary_path: "C:\\private\\Desktop\\.portrait.tmp",
      }],
    },
    taskDock,
  });
  html = surface.__testHtml();
  assert(html.includes("Binary files are approved by immutable identity"), "Binary approval semantics missing");
  assert(html.includes("Desktop/portrait.png"), "Binary preview lost its public target");
  assert(html.includes("image/png"), "Binary preview lost its media type");
  assert(html.includes("2048 bytes"), "Binary preview lost its exact size");
  assert(html.includes(binarySha), "Binary preview lost its full SHA-256");
  assert(!html.includes("C:\\private\\workspace"), "Binary preview leaked its staging source");
  assert(!html.includes(".portrait.tmp"), "Binary preview leaked its transaction path");

  surface.setPayload({
    open: true, permissionVisible: true, taskDock,
    permissionRequest: {
      id: "permission-large", workItemId: "work-preview", attemptId: "attempt-preview",
      capability: "filesystem.export", action: "copy_to_desktop",
      scope: ["C:\\Users\\person\\Desktop\\profile.html"],
      options: ["allow_once", "deny"], previewComplete: false, previewVersion: 3,
      previews: [{path: "Desktop/profile.html", status: "truncated_text",
        mediaType: "text/html", sizeBytes: 2617367, sha256: binarySha}],
    },
  });
  html = surface.__testHtml();
  assert(html.includes("Text preview truncated"), "Incomplete preview was not disclosed");
  assert(html.includes("Approval covers the complete files"), "Approval scope is unclear");
  assert(html.includes("2617367 bytes"), "Large file lost its full size");
  assert(html.includes(binarySha), "Large file lost its full hash");
  assert(html.includes('data-permission-action="allow_once"'), "Large file cannot be approved");
  assert(html.includes('data-permission-action="review_file"'), "Full file review is unavailable");
  await surface.__testPermission("review_file", "profile.html");
  const reviewBody = JSON.parse(requests[requests.length - 1].options.body);
  assert(reviewBody.action === "review_file" && reviewBody.relative_path === "profile.html", "Review lost its file selector");
  assert(reviewBody.permission_request_id === "permission-large", "Review lost its permission owner");
  assert(!("path" in reviewBody), "Renderer supplied a source path");
  assert(surface.__testHtml().includes('data-permission-action="allow_once"'), "Review consumed the pending approval");

  surface.setPayload({
    open: true,
    mode: "permission",
    permissionVisible: true,
    permissionRequest: {
      id: "permission-cooperative",
      ownerKind: "cooperative_run",
      sessionId: "session-current",
      runId: "run-cooperative",
      providerRequestId: "native-permission",
      capability: "shell.execute",
      action: "execute_command",
      scope: ["C:\\workspace\\target.txt"],
      reason: "Approve the exact provider action.",
      reversibility: "Writes one listed target.",
      options: ["allow_once", "deny"],
    },
    taskDock,
  });
  surface.setPayload({ taskDock: { ...taskDock, revision: "ledger-refresh" } });
  surface.setPayload({ permissionVisible: false, taskDock });
  html = surface.__testHtml();
  assert(html.includes("Approve the exact provider action."), "Work refresh hid cooperative permission");
  await surface.__testPermission("allow_once");
  const cooperativeBody = JSON.parse(requests[requests.length - 1].options.body);
  assert(cooperativeBody.target === "permission" && cooperativeBody.action === "allow_once", "Cooperative permission escaped permission routing");
  assert(cooperativeBody.owner_kind === "cooperative_run", "Cooperative owner identity missing");
  assert(cooperativeBody.permission_request_id === "permission-cooperative", "Host permission identity missing");
  assert(cooperativeBody.session_id === "session-current", "Session identity missing");
  assert(cooperativeBody.run_id === "run-cooperative", "Run identity missing");
  assert(cooperativeBody.provider_request_id === "native-permission", "Provider request identity missing");
  assert(!("work_item_id" in cooperativeBody) && !("attempt_id" in cooperativeBody), "Renderer fabricated Work identity");

  surface.setPayload({
    permissionVisible: true,
    permissionRequest: {
      id: "permission-newer",
      ownerKind: "cooperative_run",
      sessionId: "session-current",
      runId: "run-newer",
      providerRequestId: "native-newer",
      reason: "Newer cooperative permission.",
      options: ["deny"],
    },
  });
  surface.setPayload({
    permissionVisible: false,
    permissionRequest: {
      id: "permission-cooperative",
      ownerKind: "cooperative_run",
      sessionId: "session-current",
      runId: "run-cooperative",
      providerRequestId: "native-permission",
      options: ["deny"],
    },
  });
  html = surface.__testHtml();
  assert(html.includes("Newer cooperative permission."), "Stale resolution cleared a newer cooperative card");
  surface.setPayload({
    permissionVisible: false,
    permissionRequest: {
      id: "permission-newer",
      ownerKind: "cooperative_run",
      sessionId: "session-current",
      runId: "run-newer",
      providerRequestId: "native-newer",
      options: ["deny"],
    },
  });
  html = surface.__testHtml();
  assert(!html.includes("Newer cooperative permission."), "Matching resolution left cooperative permission visible");
  assert(html.includes('class="crt-canvas-task-striprow"') || html.includes('class="crt-canvas-task-dock"'), "Permission cleanup erased the underlying task projection");

  surface.setPayload({
    open: true,
    mode: "workflow",
    permissionVisible: false,
    workContext: { workItemId: "work-history", attemptId: "attempt-history" },
    taskDock: {
      ...taskDock,
      selectedWorkItemId: "",
      counts: { running: 0, needsAttention: 0, active: 0 },
      items: [taskDock.items[1]],
    },
  });
  html = surface.__testHtml();
  assert(html.includes("crt-canvas-task-striprow"), "Collapsed dock hid the current Canvas task strip");
  assert(html.includes("Historical task with stale attention"), "Compact strip lost the Canvas-bound WorkItem");
  assert(!html.includes("crt-canvas-task-railrow"), "Collapsed dock exposed the multi-card task rail");

  surface.setPayload({
    open: true,
    mode: "markdown",
    phase: "Result",
    title: "Codex App Server result report",
    markdown: "### Codex App Server result\nProcess: `done`\n\nDone.",
    reportView: { phase: "Result", title: "Codex App Server result report" },
  });
  html = surface.__testHtml();
  assert(html.includes("Codex result report"), "Historical result title kept the transport name");
  assert(html.includes("Codex result"), "Historical markdown heading kept the transport name");
  assert(!html.includes("Codex App Server"), "Historical Canvas copy was not normalized at display time");
  console.log("work preview renderer ok");
})().catch((error) => {
  console.error(error && (error.stack || error));
  process.exitCode = 1;
});
"""

    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        script_path = Path(handle.name)
        handle.write(prefix)
        handle.write(source)
        handle.write(suffix)
    try:
        result = subprocess.run(
            ["node", str(script_path)],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=15,
        )
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "work preview renderer ok" in result.stdout
