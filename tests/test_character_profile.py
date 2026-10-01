"""Character profile switching drives art, voice, persona and model references."""

from __future__ import annotations

from core.character_profile import active_art_dir, active_persona, active_voice, current_character, list_characters, switch_character


def test_character_profiles_expose_existing_assets() -> None:
    characters = {item["id"]: item for item in list_characters()}
    assert "kurisu" in characters
    assert "yachiyo" in characters
    assert characters["yachiyo"]["name"] == "月见八千代"
    assert characters["kurisu"]["art_dir"].endswith("assets\\companion\\kurisu")
    assert characters["yachiyo"]["voice_audio"].endswith("yachiyo_reference.wav")
    assert characters["yachiyo"]["voice_sovits_model"].endswith("yachiyo_e8_s288_l32.pth")
    assert characters["yachiyo"]["persona_file"].endswith("persona-SOUL.md")


def test_switching_character_changes_voice_persona_and_art() -> None:
    try:
        switch_character("yachiyo")
        assert current_character()["id"] == "yachiyo"
        assert active_voice()["audio"].endswith("yachiyo_reference.wav")
        assert active_voice()["sovits_model"].endswith("yachiyo_e8_s288_l32.pth")
        assert "月见八千代" in active_persona()
        assert active_art_dir().endswith("yachiyo-8emotions-companion")

        switch_character("kurisu")
        assert current_character()["id"] == "kurisu"
        assert active_voice()["audio"].endswith("kurisu_reference.wav")
        assert active_voice()["sovits_model"] == ""
        assert active_art_dir().endswith("assets\\companion\\kurisu")
    finally:
        switch_character("kurisu")


def test_tts_reference_follows_switched_character() -> None:
    import tts.pipeline as pipeline

    try:
        switch_character("yachiyo")
        assert pipeline._get_ref_audio().endswith("yachiyo_reference.wav")
        assert "ヤチヨ" in pipeline._get_ref_text()
        switch_character("kurisu")
        assert pipeline._get_ref_audio().endswith("kurisu_reference.wav")
    finally:
        switch_character("kurisu")
