from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from vts.expression_controller import ExpressionController


def test_mainline_configuration_does_not_load_a_legacy_vts_registry() -> None:
    controller = ExpressionController()
    manager = Mock()
    controller.load_registry = Mock()

    controller.configure(vts_manager=manager)

    controller.load_registry.assert_not_called()
    assert controller._registry == {}
    assert manager.on_reconnect_callback == controller._on_vts_reconnect


def test_explicit_compatibility_registry_can_still_be_loaded(tmp_path: Path) -> None:
    registry = tmp_path / "presets.json"
    registry.write_text(json.dumps({"smile": {"fade_in_sec": 0.1}}), encoding="utf-8")
    controller = ExpressionController()

    controller.configure(registry_path=str(registry))

    assert controller._registry == {"smile": {"fade_in_sec": 0.1}}
