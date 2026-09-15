"""Publish Host-owned role text; renderer acknowledgements are observation only."""
from __future__ import annotations

from core import session_manager as sm
from server.event_bus import bus
from server.handlers.chat_handler import ChatHandler
from server.handlers.session_handler import _display_text
from server.protocol import Method
from server.ws_handler import RequestHandler


class ChatRoleDelivery(RequestHandler):
    methods = [Method.CHAT_ROLE_RECEIVED]

    def __init__(self):
        self.receipts: list[dict] = []

    async def publish(self, event: dict) -> bool:
        return await self._publish(event, complete=True)

    async def publish_partial(self, event: dict) -> bool:
        return await self._publish(event, complete=False)

    async def allows(self, event: dict) -> bool:
        return (await ChatHandler._turn_allows_visible_emit(event["cause"])
            and event["session_id"] == sm.get_current_session_id())

    async def _publish(self, event: dict, *, complete: bool) -> bool:
        message_id, session_id = event["cause"], event["session_id"]
        visible_text = _display_text(event["text"])
        if not isinstance(visible_text, str) or not visible_text:
            return False
        if not await self.allows(event):
            return False
        if not complete:
            await bus.emit(Method.CHAT_TOKEN, {
                "token":visible_text, "turn_id":message_id,
                "session_id":session_id})
            return True
        self.receipts.append({"message_id":message_id, "session_id":session_id,
            "state":"published"})
        await bus.emit(Method.CHAT_ROLE_MESSAGE, {
            "message_id":message_id, "session_id":session_id, "text":visible_text})
        return True

    async def handle(self, method, params):
        if method != Method.CHAT_ROLE_RECEIVED or type(params.get("accepted")) is not bool:
            return {"accepted":False, "reason":"no_matching_display"}
        for receipt in reversed(self.receipts):
            if (receipt["message_id"] == params.get("message_id")
                    and receipt["session_id"] == params.get("session_id")
                    and receipt["state"] == "published"):
                receipt["state"] = "accepted" if params["accepted"] else "declined"
                return {"accepted":True}
        return {"accepted":False, "reason":"no_matching_display"}
