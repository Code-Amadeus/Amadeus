import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


def test_launcher_uses_repository_owned_overlay_without_external_helper(tmp_path: Path):
    from server.vn_launch_manager import VNLaunchManager

    helper = tmp_path / "tools/vn_portrait_overlay_lite.py"
    helper.parent.mkdir()
    helper.touch()
    manager = VNLaunchManager(tmp_path, runtime_start=AsyncMock(), runtime_stop=AsyncMock(),
                              runtime_status=AsyncMock(), runtime_line=AsyncMock())
    manager._publish_status = AsyncMock()
    process = Mock(pid=123, poll=Mock(return_value=None))
    manager._spawn = Mock(return_value=process)
    with patch("server.vn_launch_manager._http_health", side_effect=[False, True]):
        asyncio.run(manager._launch_overlay({"overlayHelper": str(helper)}, {}))
    args = manager._spawn.call_args.args[0]
    assert args[1] == str(tmp_path / "tools/vn_portrait_overlay_lite.py")
    assert "--legacy-helper" not in args
    assert args[args.index("--lite-dir") + 1] == str(tmp_path / "assets/companion/kurisu")
    assert not any("electron" in arg.lower() for arg in args)


def test_only_vn_playback_is_projected_and_audio_edges_keep_order():
    from server import vn_tts_bridge as bridge

    posted = []
    async def run():
        with patch.dict(bridge._SENTENCE_META, {"vn": {"overlay_url": "http://127.0.0.1:8788/reaction",
                                                      "emotion": "thinking", "display_text": "字幕"}}, clear=True), \
             patch.object(bridge, "_post_json", side_effect=lambda url, payload, timeout: posted.append(payload)):
            await bridge.publish_overlay_playback("main-chat", True)
            await asyncio.gather(bridge.publish_overlay_playback("vn", True), bridge.publish_overlay_playback("vn", False))
            await bridge.publish_overlay_subtitle("vn", "日本語", "中文")
    asyncio.run(run())
    assert [row.get("speaking") for row in posted] == [True, False, None]
    assert all(row["sentence_id"] == "vn" for row in posted)
    assert posted[0]["display_text"] == "字幕"
    assert posted[1]["display_text"] == ""
    assert posted[2]["source"] == "vn_pretranslation"
