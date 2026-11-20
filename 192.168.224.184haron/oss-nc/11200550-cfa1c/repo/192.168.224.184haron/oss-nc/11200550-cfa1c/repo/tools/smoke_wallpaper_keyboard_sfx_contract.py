from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wallpaper.scene_assets import _prepare_scenario_payload  # noqa: E402


def _lower_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value or "").lower() for value in values if str(value or "").strip()]


def _node_allows_keyboard_sfx(node: dict[str, Any], cfg: dict[str, Any]) -> bool:
    labels = _lower_list(cfg.get("keyboardSfxLabels"))
    hints = _lower_list(cfg.get("keyboardSfxResourceHints"))
    scene_ids = _lower_list(cfg.get("keyboardSfxSceneIds"))
    if not labels and not hints and not scene_ids:
        return False

    label = str(node.get("label") or "").lower()
    scene_id = str(node.get("sceneId") or "").lower()
    resource = str(node.get("resource") or "").lower()
    return (
        (bool(labels) and label in labels)
        or (bool(scene_ids) and scene_id in scene_ids)
        or (bool(hints) and any(hint in resource for hint in hints))
    )


def main() -> None:
    payload = _prepare_scenario_payload(port=8788)
    assert payload.get("enabled") is True, payload
    assert payload.get("keyboardSfxUrl"), "keyboard SFX asset URL should be exported"

    work = dict((payload.get("activities") or {}).get("work") or {})
    assert "computer use" in _lower_list(work.get("labels")), work
    assert "computer use" in _lower_list(work.get("keyboardSfxLabels")), work

    nodes = list((payload.get("graph") or {}).get("nodes") or [])
    by_label = {str(node.get("label") or "").lower(): node for node in nodes}
    assert "computer use" in by_label, [node.get("label") for node in nodes]
    assert "sleep_desk" in by_label, [node.get("label") for node in nodes]

    assert _node_allows_keyboard_sfx(by_label["computer use"], work), work
    assert not _node_allows_keyboard_sfx(by_label["sleep_desk"], work), work
    for label, node in by_label.items():
        if label not in {"computer use", "sleep_desk"}:
            assert not _node_allows_keyboard_sfx(node, work), (label, node, work)

    print("wallpaper keyboard SFX contract smoke ok")


if __name__ == "__main__":
    main()
