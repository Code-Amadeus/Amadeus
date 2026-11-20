"""Regression coverage for the Slice WorkItem disposition menu layout.

Runnable directly by tools/run_tests.py and compatible with pytest.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SURFACE = ROOT / "render" / "web" / "crt_canvas_surface.js"


def _surface_source() -> str:
    return SURFACE.read_text(encoding="utf-8")


def test_disposition_menu_is_portaled_out_of_the_scrolling_rail() -> None:
    source = _surface_source()
    assert "taskDispositionOverlayHtml(items, dock)" in source
    assert "data-work-disposition-overlay" in source
    assert "positionWorkDispositionMenu" in source
    assert ".crt-canvas-task-rail.has-open-disposition" not in source
    assert "min-height: 82px" not in source


def test_overlay_is_anchored_inside_the_canvas_boundary() -> None:
    source = _surface_source()
    assert ".crt-canvas-task-disposition-overlay" in source
    assert 'menu.closest(".crt-canvas-task-dock")' in source
    assert "const maxBottom = cardRect.bottom" in source
    assert 'open ? " is-open" : ""' in source


def test_selected_task_secondary_actions_live_inside_the_dot_menu() -> None:
    source = _surface_source()
    overlay_start = source.index("function taskDispositionOverlayHtml(items, dock)")
    overlay_end = source.index("async function handleWorkItemDisposition", overlay_start)
    overlay = source[overlay_start:overlay_end]
    dock_start = source.index("function taskDockPane()")
    dock_end = source.index("function handleTaskFilter", dock_start)
    dock = source[dock_start:dock_end]

    assert 'data-conversation-open=\\"work-item\\"' in overlay
    assert 'data-work-focus-mode=\\"pinned\\"' in overlay
    assert ">Open in Chat</button>" in overlay
    assert ">Restore</button>" in overlay
    assert 'class=\\"is-secondary\\" data-conversation-open' not in dock
    assert 'class=\\"is-secondary\\" data-work-focus-mode' not in dock


def test_task_rail_starts_collapsed_without_persisting_expanded_state() -> None:
    source = _surface_source()
    filter_start = source.index("function handleTaskFilter(button)")
    filter_end = source.index("function canvasModeTabs", filter_start)
    filter_handler = source[filter_start:filter_end]

    assert "taskRailExpanded: false" in source
    assert "state.taskRailExpanded = true" in filter_handler
    assert "loadSavedRailExpanded" not in source


def test_status_bar_owns_destination_without_history_totals() -> None:
    source = _surface_source()
    status_start = source.index("function taskDockCountText()")
    status_end = source.index("function statusText", status_start)
    status = source[status_start:status_end]
    dock_start = source.index("function taskDockPane()")
    dock_end = source.index("function handleTaskFilter", dock_start)
    dock = source[dock_start:dock_end]

    assert '["Destination", destination]' in status
    assert "taskBelongsToCurrentSession(item, dock)" in status
    assert "!taskIsHistory(item)" in status
    assert 'items.length + " tasks"' not in status
    assert "counts.needsAttention" not in status
    assert "crt-canvas-task-destination" not in dock


def test_task_filter_header_stays_compact_at_the_narrow_preset() -> None:
    source = _surface_source()

    assert ".crt-canvas-task-dock-head {" in source
    assert "gap: 4px;" in source
    assert ".crt-canvas-task-filters {" in source
    assert "gap: 2px;" in source
    assert "padding: 0 5px;" in source
    assert "width: 15px;" in source
    assert "min-width: 15px;" in source


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all work disposition menu layout tests passed")


if __name__ == "__main__":
    _main()
