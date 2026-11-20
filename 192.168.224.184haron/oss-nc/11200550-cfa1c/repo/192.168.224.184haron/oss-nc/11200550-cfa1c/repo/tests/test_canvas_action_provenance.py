"""Canvas actions must come from structured facts, never narrated prose."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SURFACE = ROOT / "render" / "web" / "crt_canvas_surface.js"


def _render_with_node(expression: str) -> str:
    script = r"""
const fs = require("fs");
const vm = require("vm");
const sourcePath = process.argv[1];
let source = fs.readFileSync(sourcePath, "utf8");
source = source.replace(
  "window.createCrtCanvasSurface = createCrtCanvasSurface;",
  "window.__canvasContract = { inlineMarkdown, fileRefHtml };\n  window.createCrtCanvasSurface = createCrtCanvasSurface;"
);
const context = { window: {}, URL };
vm.runInNewContext(source, context, { filename: sourcePath });
const value = eval(process.argv[2]);
process.stdout.write(String(value));
"""
    proc = subprocess.run(
        ["node", "-e", script, str(SURFACE), expression],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout


def test_narrated_local_paths_remain_inert_text() -> None:
    samples = [
        r'C:\Users\user\Desktop」と確認しました。移動を試みましたが拒否されました。',
        r'C:\Users\user\Desktop\game.html',
        r'`C:\Users\user\Desktop\game.html`',
        r'[game](C:\Users\user\Desktop\game.html)',
    ]
    for sample in samples:
        rendered = _render_with_node(
            f"context.window.__canvasContract.inlineMarkdown({json.dumps(sample)})"
        )
        assert "data-file-action" not in rendered
        assert "crt-canvas-file-ref" not in rendered


def test_structured_file_reference_remains_actionable() -> None:
    rendered = _render_with_node(
        r'context.window.__canvasContract.fileRefHtml("C:\\Users\\Lucas\\Desktop\\game.html")'
    )
    assert 'data-file-action="open"' in rendered
    assert 'data-file-path="C:\\Users\\Lucas\\Desktop\\game.html"' in rendered


def test_narrated_web_source_remains_a_web_action() -> None:
    rendered = _render_with_node(
        'context.window.__canvasContract.inlineMarkdown("See https://example.com/source.")'
    )
    assert "crt-canvas-web-ref" in rendered
    assert "data-file-action" not in rendered


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all canvas action provenance tests passed")


if __name__ == "__main__":
    _main()
