"""Semantic contract for one body shared by concurrent experience sources."""

from __future__ import annotations

import asyncio

from server.character_presentation import (
    CharacterPresentationCoordinator,
    PlaybackPresentationBridge,
    project_auip_update,
)
from server.protocol import Method


def test_releasing_one_source_restores_the_remaining_source() -> None:
    async def run() -> None:
        emitted: list[tuple[str, dict]] = []

        async def emit(method: str, payload: dict) -> None:
            emitted.append((method, payload))

        presentation = CharacterPresentationCoordinator(emit)
        await presentation.claim(
            source_kind="work",
            source_id="active-work",
            label="work",
        )
        await presentation.claim(
            source_kind="auip",
            source_id="app-1",
            label="surprised",
        )
        await presentation.release(source_kind="auip", source_id="app-1")

        assert [method for method, _ in emitted] == [
            Method.RENDER_SPRITEFORGE_INTENT,
            Method.RENDER_SPRITEFORGE_INTENT,
            Method.RENDER_SPRITEFORGE_INTENT,
        ]
        assert emitted[-1][1]["semantic_label"] == "work"
        assert emitted[-1][1]["presentation_source_kind"] == "work"
        assert emitted[-1][1]["presentation_source_id"] == "active-work"

    asyncio.run(run())


def test_utterance_temporarily_overrides_ambient_without_destroying_it() -> None:
    async def run() -> None:
        emitted: list[tuple[str, dict]] = []

        async def emit(method: str, payload: dict) -> None:
            emitted.append((method, payload))

        presentation = CharacterPresentationCoordinator(emit)
        await presentation.claim(
            source_kind="auip",
            source_id="app-1",
            label="work",
        )
        await presentation.claim(
            source_kind="vn",
            source_id="line-7",
            label="angry",
            tier="utterance",
        )
        await presentation.release(
            source_kind="vn",
            source_id="line-7",
            tier="utterance",
        )

        assert emitted[1][1]["presentation_tier"] == "utterance"
        assert emitted[-1][1]["presentation_source_kind"] == "auip"
        assert all(method != Method.RENDER_SPRITEFORGE_RELEASE for method, _ in emitted)

    asyncio.run(run())


def test_repeated_ambient_heartbeat_does_not_steal_from_current_source() -> None:
    async def run() -> None:
        emitted: list[tuple[str, dict]] = []

        async def emit(method: str, payload: dict) -> None:
            emitted.append((method, payload))

        presentation = CharacterPresentationCoordinator(emit)
        await presentation.claim(
            source_kind="work",
            source_id="active-work",
            label="work",
        )
        await presentation.claim(
            source_kind="auip",
            source_id="app-1",
            label="thinking",
        )
        await presentation.claim(
            source_kind="work",
            source_id="active-work",
            label="work",
            metadata={"reason": "heartbeat"},
        )

        assert len(emitted) == 2
        assert presentation.effective_owner is not None
        assert presentation.effective_owner.source_kind == "auip"

    asyncio.run(run())


def test_last_release_is_the_only_global_release() -> None:
    async def run() -> None:
        emitted: list[tuple[str, dict]] = []

        async def emit(method: str, payload: dict) -> None:
            emitted.append((method, payload))

        presentation = CharacterPresentationCoordinator(emit)
        await presentation.claim(
            source_kind="work",
            source_id="active-work",
            label="work",
        )
        await presentation.release(source_kind="work", source_id="active-work")
        await presentation.release(source_kind="work", source_id="active-work")

        releases = [item for item in emitted if item[0] == Method.RENDER_SPRITEFORGE_RELEASE]
        assert len(releases) == 1

    asyncio.run(run())


def test_auip_lifecycle_claims_and_releases_its_own_identity() -> None:
    async def run() -> None:
        emitted: list[tuple[str, dict]] = []

        async def emit(method: str, payload: dict) -> None:
            emitted.append((method, payload))

        presentation = CharacterPresentationCoordinator(emit)
        await project_auip_update(
            "auip.updated",
            {"app_session_id": "app-1", "status": "active", "stance": "spectator"},
            target=presentation,
        )
        await project_auip_update(
            "auip.updated",
            {"app_session_id": "app-1", "status": "completed"},
            target=presentation,
        )

        intents = [item for item in emitted if item[0] == Method.RENDER_SPRITEFORGE_INTENT]
        releases = [item for item in emitted if item[0] == Method.RENDER_SPRITEFORGE_RELEASE]
        scenes = [item for item in emitted if item[0] == Method.WALLPAPER_ACTIVITY]
        assert intents[0][1]["presentation_source_kind"] == "auip"
        assert intents[0][1]["presentation_source_id"] == "app-1"
        assert intents[0][1]["stance"] == "spectator"
        assert len(releases) == 1
        assert [item[1]["activity"] for item in scenes] == ["work", ""]

    asyncio.run(run())


def test_playback_claim_spans_one_multi_sentence_turn_without_reentering() -> None:
    emitted: list[tuple[str, dict]] = []

    def emit_now(method: str, payload: dict) -> None:
        emitted.append((method, payload))

    async def emit(_method: str, _payload: dict) -> None:
        raise AssertionError("playback callbacks must use the thread-safe emitter")

    presentation = CharacterPresentationCoordinator(emit, emit_now=emit_now)
    playback = PlaybackPresentationBridge(presentation)
    playback.on_sentence_start(
        "sentence-1",
        {
            "narration_source_kind": "auip",
            "narration_source_id": "app-1",
            "narration_complete_turn": True,
            "emotion": "surprised",
        },
    )
    playback.on_sentence_start("sentence-1", {})
    playback.on_sentence_end("sentence-1")
    playback.on_sentence_start(
        "sentence-2",
        {
            "narration_source_kind": "auip",
            "narration_source_id": "app-1",
            "narration_complete_turn": True,
            "emotion": "surprised",
        },
    )
    playback.on_sentence_end("sentence-2")

    assert len(emitted) == 1
    assert emitted[0][1]["presentation_source_kind"] == "auip"
    assert emitted[0][1]["presentation_source_id"] == "app-1"

    playback.release_all()
    assert len(emitted) == 2
    assert emitted[1][0] == Method.RENDER_SPRITEFORGE_RELEASE


def test_normal_playback_release_marks_the_renderer_handoff_after_speech() -> None:
    emitted: list[tuple[str, dict]] = []

    def emit_now(method: str, payload: dict) -> None:
        emitted.append((method, payload))

    async def emit(_method: str, _payload: dict) -> None:
        raise AssertionError("playback callbacks must use the thread-safe emitter")

    presentation = CharacterPresentationCoordinator(emit, emit_now=emit_now)
    playback = PlaybackPresentationBridge(presentation)
    playback.on_sentence_start(
        "sentence-1",
        {
            "narration_source_kind": "main_chat",
            "narration_source_id": "turn-1",
            "narration_complete_turn": True,
            "emotion": "smile",
        },
    )

    playback.release_all(handoff="after_speech")

    assert emitted[-1][0] == Method.RENDER_SPRITEFORGE_RELEASE
    assert emitted[-1][1]["presentation_handoff"] == "after_speech"


def test_after_speech_release_defers_the_restored_ambient_pose() -> None:
    emitted: list[tuple[str, dict]] = []

    def emit_now(method: str, payload: dict) -> None:
        emitted.append((method, payload))

    async def emit(_method: str, _payload: dict) -> None:
        raise AssertionError("synchronous claims must use emit_now")

    presentation = CharacterPresentationCoordinator(emit, emit_now=emit_now)
    presentation.claim_now(
        source_kind="work",
        source_id="active-work",
        label="work",
        tier="ambient",
    )
    presentation.claim_now(
        source_kind="main_chat",
        source_id="turn-1",
        label="smile",
        tier="utterance",
    )
    presentation.release_now(
        source_kind="main_chat",
        source_id="turn-1",
        tier="utterance",
        handoff="after_speech",
    )

    assert emitted[-1][0] == Method.RENDER_SPRITEFORGE_INTENT
    assert emitted[-1][1]["presentation_source_kind"] == "work"
    assert emitted[-1][1]["presentation_handoff"] == "after_speech"


def test_interrupt_release_remains_immediate() -> None:
    emitted: list[tuple[str, dict]] = []

    def emit_now(method: str, payload: dict) -> None:
        emitted.append((method, payload))

    async def emit(_method: str, _payload: dict) -> None:
        raise AssertionError("playback callbacks must use the thread-safe emitter")

    presentation = CharacterPresentationCoordinator(emit, emit_now=emit_now)
    playback = PlaybackPresentationBridge(presentation)
    playback.on_sentence_start(
        "sentence-1",
        {
            "narration_source_kind": "main_chat",
            "narration_source_id": "turn-1",
            "narration_complete_turn": True,
        },
    )

    playback.release_all(handoff="immediate")

    assert emitted[-1][0] == Method.RENDER_SPRITEFORGE_RELEASE
    assert "presentation_handoff" not in emitted[-1][1]


def test_sentence_scoped_playback_still_releases_at_sentence_end() -> None:
    emitted: list[tuple[str, dict]] = []

    def emit_now(method: str, payload: dict) -> None:
        emitted.append((method, payload))

    async def emit(_method: str, _payload: dict) -> None:
        raise AssertionError("playback callbacks must use the thread-safe emitter")

    presentation = CharacterPresentationCoordinator(emit, emit_now=emit_now)
    playback = PlaybackPresentationBridge(presentation)
    playback.on_sentence_start(
        "vn-sentence-1",
        {
            "narration_source_kind": "vn",
            "narration_source_id": "vn-session-1",
            "emotion": "surprised",
        },
    )
    playback.on_sentence_end("vn-sentence-1")

    assert emitted[0][1]["presentation_source_id"] == (
        "vn-session-1:vn-sentence-1"
    )
    assert emitted[1][0] == Method.RENDER_SPRITEFORGE_RELEASE


def test_work_and_auip_share_one_computer_use_scene_until_both_finish() -> None:
    async def run() -> None:
        emitted: list[tuple[str, dict]] = []

        async def emit(method: str, payload: dict) -> None:
            emitted.append((method, payload))

        presentation = CharacterPresentationCoordinator(emit)
        await presentation.claim(
            source_kind="work",
            source_id="active-work",
            label="work",
            scenario="computer-use",
        )
        await presentation.claim(
            source_kind="auip",
            source_id="app-1",
            label="work",
            scenario="computer-use",
        )
        await presentation.release(
            source_kind="work",
            source_id="active-work",
            scenario="computer-use",
        )
        scene_events = [item for item in emitted if item[0] == Method.WALLPAPER_ACTIVITY]
        assert [item[1]["activity"] for item in scene_events] == ["work"]

        await presentation.release(
            source_kind="auip",
            source_id="app-1",
            scenario="computer-use",
        )
        scene_events = [item for item in emitted if item[0] == Method.WALLPAPER_ACTIVITY]
        assert [item[1]["activity"] for item in scene_events] == ["work", ""]

    asyncio.run(run())


if __name__ == "__main__":
    test_releasing_one_source_restores_the_remaining_source()
    test_utterance_temporarily_overrides_ambient_without_destroying_it()
    test_repeated_ambient_heartbeat_does_not_steal_from_current_source()
    test_last_release_is_the_only_global_release()
    test_auip_lifecycle_claims_and_releases_its_own_identity()
    test_playback_claim_spans_one_multi_sentence_turn_without_reentering()
    test_sentence_scoped_playback_still_releases_at_sentence_end()
    test_work_and_auip_share_one_computer_use_scene_until_both_finish()
    print("ok: character presentation claims preserve one embodied surface")
