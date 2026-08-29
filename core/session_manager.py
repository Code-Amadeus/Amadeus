"""
会话管理模块
- ConversationHistory：对话历史维护（滚动窗口 + token 估算 + 摘要触发）
- 会话持久化 CRUD（JSON 文件存储）

注意：连续对话开关的运行时归属为 core.chat_runtime.ChatRuntime.enable_conversation，
通过参数传入 save_session / load_session。（历史上该开关是 main.py 的模块全局，
由已退役的 chatGui.py 直接读写。）
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ConversationHistory
# ---------------------------------------------------------------------------
class ConversationHistory:
    def __init__(self, max_rounds: int = 10, summary_token_threshold: int = 3000):
        self.dialog = []  # {"role": "user"|"assistant", "content": str, "turn_id"?: str}
        self.max_rounds = max_rounds
        # Retained in persisted Session files for backward compatibility.
        # The alpha product uses a bounded rolling window; it does not ask the
        # visible reply model to generate an in-band memory summary.
        self.summary_token_threshold = summary_token_threshold
        self.last_summary = ""

    def reset(self):
        self.dialog.clear()

    def _estimate_tokens(self, text: str) -> int:
        return len(text or "")

    def total_tokens(self) -> int:
        return sum(self._estimate_tokens(m.get("content", "")) for m in self.dialog)

    def add_user(self, content: str):
        if not content:
            return
        self.dialog.append({"role": "user", "content": content})
        self._trim()

    def add_assistant(self, content: str, turn_id: str | None = None):
        if not content:
            return
        message = {"role": "assistant", "content": content}
        if turn_id:
            message["turn_id"] = str(turn_id)
        self.dialog.append(message)
        self._trim()

    def mark_last_assistant_interrupted(
        self,
        heard_content: str,
        marker: str = "[interrupted by user]",
        turn_id: str | None = None,
    ) -> bool:
        marker = (marker or "[interrupted by user]").strip()
        heard_content = (heard_content or "").strip()
        assistants = [
            message
            for message in reversed(self.dialog)
            if message.get("role") == "assistant"
        ]
        target = None
        if turn_id:
            requested_turn_id = str(turn_id)
            target = next(
                (
                    message
                    for message in assistants
                    if str(message.get("turn_id") or "") == requested_turn_id
                ),
                None,
            )
        if target is None and assistants:
            target = assistants[0]
        if target is not None:
            committed_control = self._committed_control_text(
                str(target.get("content") or "")
            )
            content = f"{heard_content} {marker}".strip() if heard_content else marker
            # Interruption changes what the user heard, not what the Host
            # already committed. Dropping a completed DELEGATE/CONTROL tag
            # here erases the only model-visible evidence that Work started;
            # the next constraint fragment can then be misclassified as a new
            # execute. Preserve exactly one already-committed control outcome
            # after the audible interruption marker.
            if committed_control and committed_control not in content:
                content = f"{content}\n\n{committed_control}"
            if target.get("content") == content:
                return False
            target["content"] = content
            return True
        return False

    @staticmethod
    def _committed_control_text(content: str) -> str:
        """Extract one complete public control outcome from stored history."""

        if "[DELEGATE" not in content and "[CONTROL" not in content:
            return ""
        try:
            from llm.stream_parser import StreamTagParser

            parser = StreamTagParser(control_envelope_enabled=True)
            _cleaned, actions = parser.process_chunk(str(content or ""))
            return next(
                (
                    str(action.get("raw") or "").strip()
                    for action in actions
                    if str(action.get("type") or "").strip().upper()
                    in {"DELEGATE", "CONTROL"}
                    and str(action.get("raw") or "").strip()
                ),
                "",
            )
        except Exception:
            logger.debug("could not preserve interrupted control history", exc_info=True)
            return ""

    def _trim(self):
        max_items = max(2, self.max_rounds * 2)
        if len(self.dialog) > max_items:
            self.dialog = self.dialog[-max_items:]

    def build_deepseek_messages(
        self,
        system_prompt: str,
        latest_user: str,
        *,
        current_turn_system: str = "",
    ):
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        for m in self.dialog:
            messages.append({"role": m["role"], "content": m["content"]})
        # Host-owned facts about this exact turn belong after history and
        # before the user's words. They are neither durable memory nor a
        # rewrite of the user utterance, and therefore cannot be displaced by
        # stale assistant claims in the rolling window.
        if current_turn_system:
            messages.append({"role": "system", "content": current_turn_system})
        messages.append({"role": "user", "content": latest_user})
        return messages

    def build_gemini_full_prompt(
        self,
        system_prompt: str,
        latest_user: str,
        *,
        current_turn_system: str = "",
    ) -> str:
        parts = []
        if system_prompt:
            parts.append(system_prompt)
        for m in self.dialog:
            prefix = "ユーザー:" if m["role"] == "user" else "アシスタント:"
            parts.append(f"{prefix}{m['content']}")
        if current_turn_system:
            parts.append(current_turn_system)
        parts.append(f"質問:{latest_user}")
        return "\n\n".join(parts)


# 全局单例
conversation_history = ConversationHistory(max_rounds=10, summary_token_threshold=3000)

# ---------------------------------------------------------------------------
# 会话持久化
# ---------------------------------------------------------------------------
_SESSION_DIR = os.environ.get("AMADEUS_SESSION_DIR") or os.path.join(os.getcwd(), "sessions")
_CURRENT_SESSION_ID: str | None = None


def _ensure_session_dir():
    try:
        os.makedirs(_SESSION_DIR, exist_ok=True)
    except Exception:
        pass


def list_sessions() -> list[str]:
    _ensure_session_dir()
    try:
        files = [f for f in os.listdir(_SESSION_DIR) if f.endswith(".json")]
        return sorted([os.path.splitext(f)[0] for f in files])
    except Exception:
        return []


def _session_path(session_id: str) -> str:
    _ensure_session_dir()
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id or "default")
    return os.path.join(_SESSION_DIR, f"{safe}.json")


def create_session(session_id: str) -> str:
    global _CURRENT_SESSION_ID
    if not session_id:
        session_id = time.strftime("%Y%m%d-%H%M%S")
    _CURRENT_SESSION_ID = session_id
    conversation_history.reset()
    save_session(session_id)
    return session_id


def save_session(session_id: str = None, *, enable_conversation: bool = False):
    """
    持久化当前会话到 JSON 文件。

    参数：
        enable_conversation: 当前 ENABLE_CONVERSATION 运行时状态，由调用方传入。
    """
    sid = session_id or _CURRENT_SESSION_ID
    if not sid:
        return
    existing_title = None
    path = _session_path(sid)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing_title = json.load(f).get("title")
        except Exception:
            pass
    data = {
        "session_id": sid,
        "dialog": conversation_history.dialog,
        "last_summary": getattr(conversation_history, "last_summary", ""),
        "max_rounds": conversation_history.max_rounds,
        "summary_token_threshold": conversation_history.summary_token_threshold,
        "enable_conversation": enable_conversation,
        "timestamp": time.time(),
    }
    if existing_title:
        data["title"] = existing_title
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
            logger.error(f"failed to save session: {e}")


def load_session(session_id: str) -> tuple[bool, bool]:
    """
    从 JSON 文件加载会话，返回 (success, enable_conversation)。
    调用方负责将 enable_conversation 写回自己的全局变量。
    """
    global _CURRENT_SESSION_ID
    path = _session_path(session_id)
    if not os.path.exists(path):
        return False, False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        conversation_history.dialog = data.get("dialog", [])
        conversation_history.last_summary = data.get("last_summary", "")
        conversation_history.max_rounds = int(data.get("max_rounds", conversation_history.max_rounds))
        conversation_history.summary_token_threshold = int(
            data.get("summary_token_threshold", conversation_history.summary_token_threshold)
        )
        _CURRENT_SESSION_ID = data.get("session_id", session_id)
        return True, bool(data.get("enable_conversation", False))
    except Exception:
        logger.error("runtime log event at core/session_manager.py:209")
        return False, False


def delete_session(session_id: str) -> bool:
    try:
        path = _session_path(session_id)
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception:
        logger.error("runtime log event at core/session_manager.py:220")
    return False


def rename_session(old_id: str, new_id: str) -> bool:
    try:
        old_path = _session_path(old_id)
        new_path = _session_path(new_id)
        if os.path.exists(old_path):
            os.replace(old_path, new_path)
            return True
    except Exception:
        logger.error("runtime log event at core/session_manager.py:232")
    return False


def get_current_session_id() -> str | None:
    return _CURRENT_SESSION_ID


def set_current_session_id(session_id: str) -> None:
    global _CURRENT_SESSION_ID
    _CURRENT_SESSION_ID = session_id


def get_session_title(session_id: str) -> str:
    path = _session_path(session_id)
    if not os.path.exists(path):
        return session_id
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("title") or session_id
    except Exception:
        return session_id


def set_session_title(session_id: str, title: str) -> bool:
    path = _session_path(session_id)
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["title"] = title
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        logger.error("runtime log event at core/session_manager.py:269")
        return False


async def generate_session_title(first_user_message: str) -> str:
    """Derive a local fallback title without contacting an undeclared Provider.

    The current Electron path does not call this compatibility helper, but a
    caller must never upload the first user message to a hard-coded service as
    a side effect of naming a local session.
    """

    compact = " ".join(str(first_user_message or "").split())
    return compact[:30].strip('"\'「」《》 ')
