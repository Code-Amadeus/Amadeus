"""
会话管理模块
- ConversationHistory：会话完整转录（持久化权威）+ 模型侧有界提示窗口
- 会话持久化 CRUD（JSON 文件存储）

契约：Session JSON 是会话历史的权威记录，保存完整转录；模型只消费最近
max_rounds 轮的投影（见 ConversationHistory._prompt_window）。持久化与
界面展示不得因窗口裁剪而丢失更早的轮次。

注意：连续对话开关的运行时归属为 core.chat_runtime.ChatRuntime.enable_conversation，
通过参数传入 save_session / load_session。（历史上该开关是 main.py 的模块全局，
由已退役的 chatGui.py 直接读写。）
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging
import os
import re
import tempfile
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ConversationHistory
# ---------------------------------------------------------------------------
class ConversationHistory:
    def __init__(self, max_rounds: int = 10, summary_token_threshold: int = 3000):
        # 完整会话转录：持久化、界面回放与恢复都以它为准。模型提示只取
        # 末尾 max_rounds 轮的投影，裁剪从不写回这个列表。
        self.dialog: list[dict[str, Any]] = []  # {role, content, turn_id?, message_id?}
        self.max_rounds = max_rounds
        # Retained in persisted Session files for backward compatibility.
        # The alpha product uses a bounded prompt window; it does not ask the
        # visible reply model to generate an in-band memory summary.
        self.summary_token_threshold = summary_token_threshold
        self.last_summary = ""

    def reset(self):
        self.dialog.clear()

    def snapshot(self) -> ConversationHistory:
        """Detach one turn's input from later active-Session mutations."""

        history = ConversationHistory(self.max_rounds, self.summary_token_threshold)
        history.dialog = deepcopy(self.dialog)
        history.last_summary = self.last_summary
        return history

    def _estimate_tokens(self, text: str) -> int:
        return len(text or "")

    def total_tokens(self) -> int:
        return sum(self._estimate_tokens(m.get("content", "")) for m in self.dialog)

    def add_user(self, content: str):
        if not content:
            return
        self.dialog.append({"role": "user", "content": content})

    def add_assistant(self, content: str, turn_id: str | None = None):
        if not content:
            return
        message = {"role": "assistant", "content": content}
        if turn_id:
            message["turn_id"] = str(turn_id)
        self.dialog.append(message)

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
            recorded_controls = self._recorded_control_text(
                str(target.get("content") or "")
            )
            content = f"{heard_content} {marker}".strip() if heard_content else marker
            # Audio interruption changes what was heard, not the recorded
            # control sequence. Keep every complete record, in order and
            # with its original multiplicity. This is history projection,
            # not another dispatch, deduplication or acceptance decision.
            if recorded_controls and recorded_controls not in content:
                content = f"{content}\n\n{recorded_controls}"
            if target.get("content") == content:
                return False
            target["content"] = content
            return True
        return False

    @staticmethod
    def _recorded_control_text(content: str) -> str:
        """Extract the ordered public controls already present in history."""

        control_types = {"DELEGATE", "CONTROL", "AUIP"}
        if not any(f"[{kind}" in content.upper() for kind in control_types):
            return ""
        try:
            from llm.stream_parser import StreamTagParser

            parser = StreamTagParser(control_envelope_enabled=True, stop_after_control=False)
            _cleaned, actions = parser.process_chunk(str(content or ""))
            return "".join(
                str(action.get("raw") or "").strip()
                for action in actions
                if str(action.get("type") or "").strip().upper() in control_types
                and str(action.get("raw") or "").strip()
            )
        except Exception:
            logger.debug("could not preserve interrupted control history", exc_info=True)
            return ""

    def _prompt_window(self) -> list[dict[str, Any]]:
        """Return the model-visible projection: the most recent max_rounds rounds.

        The projection is read-only. Older accepted turns stay in ``dialog`` for
        persistence, Session reload, Continuity archive recall and crash
        recovery; only the prompt is bounded.
        """

        return self.dialog[-max(2, self.max_rounds * 2):]

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
        for m in self._prompt_window():
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
        for m in self._prompt_window():
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
_SESSION_SELECTION_REVISION = 0
_activation_guard: Callable[[str | None, str | None], None] | None = None


def configure_activation_guard(
    guard: Callable[[str | None, str | None], None] | None,
) -> None:
    """Install one synchronous Host boundary before active context replacement.

    This is not a notification bus. The guard raises to refuse activation and
    must persist any required lifecycle fence before returning. Runtime wiring
    owns installation/quiescence; the default remains unconfigured.
    """
    global _activation_guard
    _activation_guard = guard


def _empty_history() -> ConversationHistory:
    return ConversationHistory(
        conversation_history.max_rounds, conversation_history.summary_token_threshold,
    )


def _check_activation(session_id: str | None) -> None:
    if _activation_guard is not None:
        result = _activation_guard(_CURRENT_SESSION_ID, session_id)
        if result is not None:
            # An async guard cannot be silently ignored by a synchronous owner.
            import inspect

            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("Session activation guard must synchronously return None")


def _install_session_state(
    session_id: str | None, history: ConversationHistory | None, *, independent_selection: bool = True,
) -> None:
    global _CURRENT_SESSION_ID, _SESSION_SELECTION_REVISION
    if history is not None:
        # Keep the established singleton object for its existing importers.
        conversation_history.dialog = history.dialog
        conversation_history.last_summary = history.last_summary
        conversation_history.max_rounds = history.max_rounds
        conversation_history.summary_token_threshold = history.summary_token_threshold
    _CURRENT_SESSION_ID = session_id
    if independent_selection:
        _SESSION_SELECTION_REVISION += 1


def _activate_session(
    session_id: str | None, history: ConversationHistory | None = None,
    *, expected_selection_revision: int | None = None,
) -> None:
    """Publish an already prepared context; failed preparation never gets here."""
    require_session_selection(expected_selection_revision)
    if session_id == _CURRENT_SESSION_ID and history is None:
        return
    _check_activation(session_id)
    _install_session_state(
        session_id, history, independent_selection=expected_selection_revision is None,
    )


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
    if not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9_-]+", session_id) is None:
        # Identity must survive create -> filename index -> load unchanged.
        # Sanitizing identifiers silently aliases distinct authority owners.
        raise ValueError("Session id must contain only ASCII letters, digits, '_' or '-'")
    _ensure_session_dir()
    path = os.path.join(_SESSION_DIR, f"{session_id}.json")
    if os.path.basename(os.path.realpath(path)) != f"{session_id}.json":
        # Windows filename lookup is case-insensitive; Session/Project keys
        # are not. Never delete A's file while clearing a different 'a' key.
        raise ValueError("Session filename does not match the requested identity")
    return path


def create_session(session_id: str, *, activate: bool = True) -> str:
    if not session_id:
        session_id = time.strftime("%Y%m%d-%H%M%S")
    history = _empty_history()
    _persist_history(session_id, history, enable_conversation=False, create=True)
    if not activate:
        return session_id
    try:
        _activate_session(session_id, history)
    except BaseException:
        # This invocation exclusively created the new file. A refused active
        # transition must not leave a newly created empty Session in the index.
        if not delete_session(session_id):
            logger.error("could not remove unactivated Session %r", session_id)
        raise
    return session_id


def save_session(session_id: str | None = None, *, enable_conversation: bool = False) -> bool:
    """
    持久化当前会话到 JSON 文件。

    参数：
        enable_conversation: 当前 ENABLE_CONVERSATION 运行时状态，由调用方传入。
    """
    sid = session_id or _CURRENT_SESSION_ID
    if not sid:
        return False
    if sid != _CURRENT_SESSION_ID:
        # This API persists the loaded history, not an arbitrary Session.
        # A late turn may still name A after B is loaded; never save B as A.
        logger.warning("skip Session save: target %r is not loaded", sid)
        return False
    history = conversation_history.snapshot()
    try:
        _persist_history(sid, history, enable_conversation=enable_conversation)
        return True
    except Exception:
        logger.exception("failed to save Session %r", sid)
        return False


def _persist_history(
    sid: str, history: ConversationHistory, *, enable_conversation: bool, create: bool = False,
) -> None:
    """Persist prepared data without relabelling the currently loaded history."""
    existing_title = None
    path = _session_path(sid)
    if not create and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing_title = json.load(f).get("title")
        except Exception:
            pass
    data = {
        "session_id": sid,
        "dialog": history.dialog,
        "last_summary": history.last_summary,
        "max_rounds": history.max_rounds,
        "summary_token_threshold": history.summary_token_threshold,
        "enable_conversation": enable_conversation,
        "timestamp": time.time(),
    }
    if existing_title:
        data["title"] = existing_title
    # Serialization failure must not truncate the last good Session file.
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    if create:
        created = False
        try:
            with open(path, "x", encoding="utf-8") as output:
                created = True
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            if created:
                try:
                    os.remove(path)
                except OSError:
                    logger.exception("could not remove incomplete new Session %r", sid)
            raise
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(path),
            prefix=".session-", suffix=".tmp", delete=False,
        ) as output:
            temporary = output.name
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.remove(temporary)


def _read_session_history(session_id: str) -> tuple[ConversationHistory, bool]:
    with open(_session_path(session_id), "r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict) or data.get("session_id", session_id) != session_id:
        raise ValueError("Session data does not match the requested identity")
    history = ConversationHistory(
        int(data.get("max_rounds", conversation_history.max_rounds)),
        int(data.get("summary_token_threshold", conversation_history.summary_token_threshold)),
    )
    history.dialog = data.get("dialog", [])
    history.last_summary = data.get("last_summary", "")
    if (
        not isinstance(history.dialog, list)
        or any(
            not isinstance(message, dict)
            or not isinstance(message.get("role"), str)
            or not isinstance(message.get("content"), str)
            for message in history.dialog
        )
        or not isinstance(history.last_summary, str)
    ):
        raise ValueError("Session history has an invalid shape")
    return history, bool(data.get("enable_conversation", False))


def append_session_message(session_id: str, *, role: str, content: str, turn_id: str,
                           message_id: str = "") -> bool:
    """Project a Host-correlated message into its existing Session.

    Called synchronously on the owning Host loop, like Session selection/save.
    This never activates or creates a Session. Replay detection covers the
    retained transcript; it is not a durable execution/input ledger.
    A turn may publish several messages. Callers with a stable message identity
    retain each part while keeping its original turn correlation.
    """
    if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip() or not turn_id:
        return False
    try:
        content.encode("utf-8", errors="strict")
        history, enable = _read_session_history(session_id)
        if session_id == _CURRENT_SESSION_ID:
            history = conversation_history.snapshot()
        message = {"role":role, "content":content, "turn_id":str(turn_id),
            **({"message_id":message_id} if message_id else {})}
        prior = next((row for row in history.dialog if row.get("role") == role
            and row.get("turn_id") == str(turn_id)
            and (not message_id or row.get("message_id") == message_id
                or (not row.get("message_id") and row.get("content") == content))), None)
        if prior is not None:
            if prior.get("content") != content:
                return False
        else:
            history.dialog.append(message)
        _persist_history(session_id, history, enable_conversation=enable)
        if session_id == _CURRENT_SESSION_ID:
            conversation_history.dialog = history.dialog
        return True
    except Exception:
        logger.exception("failed to append a message to Session %r", session_id)
        return False


def load_session(session_id: str, *, expected_selection_revision: int | None = None) -> tuple[bool, bool]:
    """
    从 JSON 文件加载会话，返回 (success, enable_conversation)。
    调用方负责将 enable_conversation 写回自己的全局变量。
    """
    try:
        if not os.path.exists(_session_path(session_id)):
            return False, False
        history, enable = _read_session_history(session_id)
        _activate_session(session_id, history, expected_selection_revision=expected_selection_revision)
        return True, enable
    except Exception:
        logger.exception("failed to load Session %r", session_id)
        return False, False


def delete_session(session_id: str) -> bool:
    try:
        path = _session_path(session_id)
        if os.path.exists(path):
            active = session_id == _CURRENT_SESSION_ID
            empty = _empty_history() if active else None
            if active:
                # Fence before removing the active file. If removal then fails,
                # keep the context but do not revive already retired authority.
                _check_activation(None)
            os.remove(path)
            if active:
                _install_session_state(None, empty)
            return True
    except Exception:
        logger.exception("failed to delete Session %r", session_id)
    return False


def get_current_session_id() -> str | None:
    return _CURRENT_SESSION_ID


def get_session_selection_revision() -> int:
    """Version of successful independent selections, including same-id reloads.

    Queued Chat inputs capture this Host fact before waiting. Their own derived
    installations consume it instead of invalidating later queued inputs. It is
    not an installation counter, a durable epoch or execution permission.
    """
    return _SESSION_SELECTION_REVISION


def require_session_selection(expected_revision: int | None) -> None:
    """Refuse a queued context consumer after an independent Session selection."""
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision != _SESSION_SELECTION_REVISION
    ):
        raise RuntimeError("Session context changed before the queued input was admitted")


def set_current_session_id(session_id: str | None) -> None:
    # Nonempty relabelling is a legacy low-level API, not Session selection.
    # Normal ingress must load/create the actual owned history first.
    _activate_session(session_id, _empty_history() if session_id is None else None)


def get_session_title(session_id: str) -> str:
    try:
        path = _session_path(session_id)
        if not os.path.exists(path):
            return session_id
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("title") or session_id
    except Exception:
        return session_id


def set_session_title(session_id: str, title: str) -> bool:
    try:
        path = _session_path(session_id)
        if not os.path.exists(path):
            return False
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
