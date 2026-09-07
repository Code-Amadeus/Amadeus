import asyncio
from unittest.mock import AsyncMock

from server.companion_audio_activity import protected_audio_apps, CompanionAudioActivity


def test_protected_apps_include_meeting_output_but_not_unrelated_audio_or_amadeus_mic():
    sessions = [{"process":"C:\\Apps\\Codex.exe","flow":"render"}, {"process":"python.exe","flow":"capture"},
                {"process":"Weixin.exe","flow":"capture"}, {"process":"Zoom.exe","flow":"render"}]
    assert protected_audio_apps(sessions) == ["Zoom 音频"]
    sessions.extend([{"process":"CODEX.EXE","flow":"capture"},{"process":"wemeetapp.exe","flow":"capture"}])
    assert protected_audio_apps(sessions) == ["Codex 语音输入", "Zoom 音频", "腾讯会议音频"]


def test_probe_failures_are_visible_and_active_transition_defers_once():
    async def run():
        blocked = AsyncMock()
        guard = CompanionAudioActivity(blocked, probe=lambda:[{"process":"Codex.exe","flow":"capture"}])
        await guard.refresh(); await guard.refresh()
        assert guard.snapshot["blocked"] is True
        blocked.assert_awaited_once()
        guard.probe = lambda: []
        guard.last_busy = 0
        await guard.refresh()
        assert guard.snapshot["blocked"] is False
        guard.probe = lambda: (_ for _ in ()).throw(OSError("probe failed"))
        await guard.refresh()
        assert guard.snapshot["status"] == "unavailable"
        assert guard.snapshot["note"]
    asyncio.run(run())
