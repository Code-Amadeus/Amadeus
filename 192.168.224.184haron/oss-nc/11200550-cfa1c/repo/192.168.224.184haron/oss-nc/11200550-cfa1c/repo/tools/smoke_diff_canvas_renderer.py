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
            """
const window = globalThis;
window.location = { origin: "http://127.0.0.1:17777", search: "" };
window.setInterval = function () {};
window.setTimeout = function () {};
window.clearTimeout = function () {};
window.localStorage = { getItem() { return null; }, setItem() {} };
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
    querySelectorAll() { return []; },
    addEventListener() {},
    setAttribute() {},
    appendChild() {},
  };
}
"""
        )
        tmp.write(source.read_text(encoding="utf-8"))
        tmp.write(
            """
const root = {
  clientWidth: 1000,
  clientHeight: 700,
  style: { setProperty() {} },
  appendChild() {},
};
const surface = window.createCrtCanvasSurface();
surface.setPayload({
  mode: "markdown",
  phase: "Result",
  title: "Codex result report",
  lead: "Everything passed.",
  markdown: "### Result Report\\nEverything passed.",
  open: true
});
surface.setPayload({
  mode: "diff",
  phase: "Preview",
  title: "Codex diff preview",
  reportMarkdown: "### Result Report\\nEverything passed.",
  diff: {
    fileCount: 2,
    additions: 2,
    deletions: 1,
    files: [
      {
        path: "app.py",
        status: "modified",
        additions: 1,
        deletions: 1,
        hunks: [{
          header: "@@ -10 +10 @@",
          lines: [
            { kind: "remove", oldLine: 10, text: "old value" },
            { kind: "add", newLine: 10, text: "new value" }
          ]
        }]
      },
      {
        path: "new.py",
        status: "untracked",
        additions: 1,
        deletions: 0,
        hunks: [{
          header: "@@ -0,0 +1 @@",
          lines: [{ kind: "add", newLine: 1, text: "created" }]
        }]
      }
    ]
  },
  open: true
});
let html = surface.__testHtml();
if (!html.includes("crt-canvas-pane diff") || !html.includes("crt-canvas-diff-line remove") || !html.includes("crt-canvas-diff-line add")) {
  throw new Error("diff rows were not rendered: " + html.slice(0, 500));
}
if (!html.includes("app.py") || !html.includes("old value") || !html.includes("new value")) {
  throw new Error("diff content is incomplete");
}
if (!html.includes("show-report") || html.includes("Everything passed.")) {
  throw new Error("report/diff mode separation failed");
}
surface.__testMode("markdown");
html = surface.__testHtml();
if (!html.includes("Everything passed.") || !html.includes("show-diff")) {
  throw new Error("report mode did not preserve the report");
}
if (!html.includes("RESULT / DOC") || !html.includes("Codex result report")) {
  throw new Error("report mode did not restore its header context");
}
surface.__testMode("diff");
surface.__testFile(1);
html = surface.__testHtml();
if (!html.includes("new.py") || !html.includes("created")) {
  throw new Error("diff file switching did not render the selected file");
}
console.log("diff canvas renderer smoke ok");
"""
        )

    try:
        text = tmp_path.read_text(encoding="utf-8")
        marker = "      layout(bounds) {\n"
        hooks = (
            "      __testHtml() { return card.innerHTML; },\n"
            "      __testMode(mode) { switchCanvasMode(mode); },\n"
            "      __testFile(index) { state.activeDiffFile = index; render(); },\n"
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
