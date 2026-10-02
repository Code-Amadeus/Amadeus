"""Game/Comic scene context contracts for the standalone Companion."""

from __future__ import annotations

from pathlib import Path

from core.companion.scenes import SceneStore


def test_scene_store_binds_captures_and_closes(tmp_path: Path) -> None:
    store = SceneStore(tmp_path / "scene.json")
    scene = store.bind("comic", title="Manga", process_name="msedge.exe")
    assert scene.kind == "comic"
    store.record_capture("第一页：两个人在雨中重逢。")
    store.append_turn("这页讲了什么", "他们在雨中重逢。")

    block = store.context_block("这页讲了什么")
    assert "<scene_context>" in block
    assert "kind=comic" in block
    assert "第一页：两个人在雨中重逢。" in block
    assert "后续页是未来内容" in block
    assert "他们在雨中重逢。" in block

    store.close()
    assert store.status()["active"] is False


def test_game_scene_context_keeps_future_secrets(tmp_path: Path) -> None:
    store = SceneStore(tmp_path / "scene.json")
    store.bind("game", title="Sample Game")
    store.record_capture("当前画面：主角站在车站前，选项一/选项二。")

    block = store.context_block("我该选哪个")
    assert "kind=game" in block
    assert "不要剧透尚未出现的剧情" in block
    assert "选项一/选项二" in block
