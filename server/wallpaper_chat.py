"""Wallpaper controls reuse the same Session, ASR and model capability owners as Chat."""

from server.protocol import Method


async def wallpaper_chat_control(
    payload: dict, *, session, asr, system, wake, voice_start, memory_status=None,
) -> dict:
    action = payload.get("action")
    source = "wake"
    if action == "status":
        config = await system.handle(Method.SYSTEM_GET_CONFIG, {})
        memory = memory_status() if memory_status is not None else {}
        return {"ok": True, "supports_images": bool(config.get("chat_supports_images")),
                "watching": bool(config.get("vision_enabled") and config.get("vision_mode") == "watching"),
                "auip_interaction_enabled": config.get("auip_interaction_enabled"),
                "memory_use_enabled": bool(memory.get("sdk_version") and memory.get("use_enabled")),
                "voice": asr.listening_state(), "wake": await wake.status()}
    if action == "auip_set":
        enabled = payload.get("enabled")
        if type(enabled) is not bool:
            return {"ok": False, "error": "invalid_application_interaction_setting"}
        result = await system.handle(Method.SYSTEM_SET_CONFIG, {"values": {"auip_interaction_enabled": enabled}})
        applied = result.get("values", {}).get("auip_interaction_enabled")
        if applied is not enabled:
            return {"ok": False, "error": "application_interaction_setting_unconfirmed"}
        return {"ok": True, "auip_interaction_enabled": applied}
    if action in {"vision_toggle", "vision_windows", "vision_select"}:
        config = await system.handle(Method.SYSTEM_GET_CONFIG, {})
        if not config.get("chat_supports_images"):
            return {"ok": False, "error": "current_model_does_not_support_images"}
        if action == "vision_windows":
            result = await system.handle(Method.SYSTEM_LIST_WINDOWS, {"limit": 48})
            return {"ok": True, **result}
        watching = bool(config.get("vision_enabled") and config.get("vision_mode") == "watching")
        if action == "vision_toggle":
            values = {"vision_enabled": False} if watching else {
                "vision_enabled": True, "vision_mode": "watching", "vision_scope": "full_screen",
            }
        else:
            handle = str(payload.get("hwnd") or "")
            values = {"vision_enabled": True, "vision_mode": "watching" if watching else "on_demand",
                      "vision_scope": "selected_window" if handle else "full_screen",
                      "vision_window_handle": handle}
        await system.handle(Method.SYSTEM_SET_CONFIG, {"values": values})
        return {"ok": True}
    if action in {"voice_stop", "new_chat"}:
        await asr.handle(Method.ASR_STOP, {"source": source})
        if action == "voice_stop":
            return {"ok": True}
        return await session.handle(Method.SESSION_CREATE, {"source": "wallpaper_keyboard"})
    if action == "voice_start":
        from config.settings import WAKE_AUTO_SEND_TO_CHAT
        if not WAKE_AUTO_SEND_TO_CHAT:
            return {"ok": False, "error": "voice_auto_send_disabled"}
        state = asr.listening_state()
        if state["active"] and state["source"] != "wake":
            return {"ok": False, "error": "already_listening"}
        current = await session.ensure_current_session(source=source)
        if current.get("ok") is not True:
            return current
        result = await voice_start()
        if result.get("status") not in {"listening", "awake"}:
            return {"ok": False, "error": result.get("error") or result.get("status")}
        return {"ok": True}
    return {"ok": False, "error": "unsupported_chat_action"}
