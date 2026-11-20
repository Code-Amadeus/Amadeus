"""Build a browser-openable overview for generated Amadeus Mermaid views."""

from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VIEW_DIR = ROOT / "architecture" / "views"
TEMPLATE = ROOT / "architecture" / "preview_template.html"
DEFAULT_OUTPUT = ROOT / "runtime" / "architecture_preview.html"

DISPLAY_NAMES = {
    "authority-boundaries.mmd": "Authority boundaries",
    "state-work-item.mmd": "WorkItem lifecycle",
    "state-run-attempt.mmd": "RunAttempt lifecycle",
    "state-permission-request.mmd": "Permission lifecycle",
    "state-observer-narration.mmd": "Observer narration cadence",
    "sequence-provider-work-and-narration.mmd": "Provider work and narration",
    "sequence-desktop-export-permission.mmd": "Desktop export permission",
}


def _diagram_files() -> list[Path]:
    paths = sorted(VIEW_DIR.glob("*.mmd"))
    if not paths:
        raise FileNotFoundError(
            "No Mermaid views found. Run tools/architecture/generate_views.py first."
        )
    order = {name: index for index, name in enumerate(DISPLAY_NAMES)}
    return sorted(paths, key=lambda path: (order.get(path.name, len(order)), path.name))


def _build_navigation(paths: list[Path]) -> str:
    items: list[str] = []
    for index, path in enumerate(paths):
        diagram_id = f"diagram-{index + 1}"
        label = html.escape(DISPLAY_NAMES.get(path.name, path.stem.replace("-", " ").title()))
        selected = "true" if index == 0 else "false"
        items.append(
            f'<button type="button" role="tab" aria-selected="{selected}" '
            f'aria-controls="{diagram_id}" data-target="{diagram_id}">{label}</button>'
        )
    return "\n".join(items)


def _build_panels(paths: list[Path]) -> str:
    panels: list[str] = []
    for index, path in enumerate(paths):
        diagram_id = f"diagram-{index + 1}"
        label = html.escape(DISPLAY_NAMES.get(path.name, path.stem.replace("-", " ").title()))
        source = html.escape(path.read_text(encoding="utf-8"))
        hidden = "" if index == 0 else " hidden"
        panels.append(
            f'<section id="{diagram_id}" role="tabpanel" class="diagram-panel"{hidden}>'
            f'<div class="diagram-heading"><h2>{label}</h2>'
            f'<code>architecture/views/{html.escape(path.name)}</code></div>'
            f'<div class="diagram-host" role="img" aria-label="{label}">'
            '<p class="loading">Rendering diagram…</p></div>'
            f'<pre class="diagram-source" hidden>{source}</pre>'
            "</section>"
        )
    return "\n".join(panels)


def build(output: Path) -> Path:
    paths = _diagram_files()
    template = TEMPLATE.read_text(encoding="utf-8")
    rendered = (
        template.replace("__DIAGRAM_NAVIGATION__", _build_navigation(paths))
        .replace("__DIAGRAM_PANELS__", _build_panels(paths))
        .replace("__DIAGRAM_COUNT__", str(len(paths)))
        .replace(
            "__GENERATED_AT__",
            html.escape(datetime.now().astimezone().isoformat(timespec="seconds")),
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output
    if not output.is_absolute():
        output = ROOT / output
    try:
        result = build(output.resolve())
    except Exception as exc:
        print(f"architecture preview failed: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
