"""
会话管理模块
- ConversationHistory：完整会话记录 + 发给模型的最近若干轮窗口
- 会话持久化 CRUD（JSON 文件存储）

当前 Chat 始终使用所选 Session 的对话历史；旧 enable_conversation 字段仅作文件兼容，
通过参数传入 save_session / load_session。（历史上该开关是 main.py 的模块全局，
由已退役的 chatGui.py 直接读写。）
"""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass
from collections import OrderedDict
from datetime import datetime
import json
import logging
import os
import re
import time
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from config.durable_io import write_text
from llm import character_prompts
from core import session_source_journal as _source_journal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ConversationHistory
# ---------------------------------------------------------------------------
# Main Chat sends at most this many recent rounds to a model. The Session keeps
# every message; this window bounds model input, not what is retained.
PROMPT_HISTORY_ROUNDS = 30


def assistant_turn_message_id(turn_id: str) -> str:
    """Identity of the single foreground reply, derived only from Host turn identity.

    This lets an interruption arriving before final publication name that same
    reply. Additional Host publications in the turn allocate independent ids.
    """
    if not turn_id:
        raise ValueError("A foreground reply requires a Host turn identity")
    return "message-" + uuid5(NAMESPACE_URL, "amadeus:assistant-turn:" + turn_id).hex


def new_message_metadata(*, message_id: str = "", legacy: bool = False) -> dict[str, Any]:
    """Allocate Host identity once; a legacy row's original time is unknown."""
    created_at_ms = None if legacy else time.time_ns() // 1_000_000
    offset = (None if created_at_ms is None else
              datetime.fromtimestamp(created_at_ms / 1000).astimezone().utcoffset())
    return {
        "message_id": message_id or "message-" + uuid4().hex,
        "created_at_ms": created_at_ms,
        "utc_offset_minutes": None if offset is None else int(offset.total_seconds() // 60),
        "revision": 1,
    }


def _normalize_message_metadata(dialog: list[dict[str, Any]]) -> bool:
    """Upgrade old rows without deriving identities or dates from their text."""
    changed = False
    identities: set[str] = set()
    for index, message in enumerate(dialog):
        identity = message.get("message_id")
        # Persistence stages share unchanged rows with the serial state owner.
        # Upgrade only detached rows, so a failed commit cannot allocate ids or
        # publish defaults into the active history.
        if (not identity or (isinstance(identity, str) and
                re.fullmatch(r"cooperative-role:[0-9a-f]{64}", identity))
                or any(name not in message for name in
                    ("created_at_ms", "utc_offset_minutes", "revision"))):
            message = dict(message)
            dialog[index] = message
        # This was the shipped cooperative delivery fallback. Retire its
        # content-derived identity before exposing a source to memory readers.
        if not identity or (isinstance(identity, str) and
                re.fullmatch(r"cooperative-role:[0-9a-f]{64}", identity)):
            message["message_id"] = "message-" + uuid4().hex
            changed = True
        identity = message["message_id"]
        if not isinstance(identity, str) or not identity.strip() or identity in identities:
            raise ValueError("Session message identity is invalid or duplicated")
        identities.add(identity)
        for name, default in (("created_at_ms", None), ("utc_offset_minutes", None), ("revision", 1)):
            if name not in message:
                message[name] = default
                changed = True
        if type(message["revision"]) is not int or message["revision"] < 1:
            raise ValueError("Session message revision must be a positive integer")
        created = message["created_at_ms"]
        offset = message["utc_offset_minutes"]
        if created is not None and (type(created) is not int or created < 0):
            raise ValueError("Session message time must be UTC milliseconds or unknown")
        if offset is not None and (type(offset) is not int or not -1440 < offset < 1440):
            raise ValueError("Session message UTC offset is invalid")
        if created is None and offset is not None:
            raise ValueError("An unknown message time cannot have a known UTC offset")
    return changed


@dataclass(frozen=True)
class _SessionFileState:
    """Small persisted header, valid only while the same file is unchanged."""

    path: str
    signature: tuple[int, ...]
    character_id: str
    title: Any
    enable_conversation: bool


class ConversationHistory:
    def __init__(self, max_rounds: int = PROMPT_HISTORY_ROUNDS, summary_token_threshold: int = 3000,
                 *, character_id: str | None = None, session_id: str | None = None):
        self._session_id = session_id
        self._character_id = character_prompts.validate_character_id(
            character_prompts.active_character_id() if character_id is None else character_id)
        # Complete Session records include Host identity, time and revision.
        self.dialog: list[dict[str, Any]] = []
        # Rounds visible to a model; see recent().
        self.max_rounds = max_rounds
        # Retained in persisted Session files for backward compatibility.
        # The alpha product sends a bounded recent window; it does not ask the
        # visible reply model to generate an in-band memory summary.
        self.summary_token_threshold = summary_token_threshold
        self.last_summary = ""
        self._file_state: _SessionFileState | None = None
        self._source_journal = None

    @property
    def character_id(self) -> str:
        """Immutable owner of this conversation, independent of editable persona."""
        return self._character_id

    def reset(self):
        self.dialog.clear()

    def snapshot(self) -> ConversationHistory:
        """Detach one turn's input from later active-Session mutations."""

        history = copy(self)
        history.dialog = deepcopy(self.dialog)
        history._file_state = deepcopy(self._file_state)
        return history

    def _persistence_copy(self) -> ConversationHistory:
        """Stage a serial-owner write; changed rows must be replaced, not edited."""
        history = copy(self)
        history.dialog = list(self.dialog)
        return history

    def _memory_incarnation(self) -> str | None:
        if self._source_journal is not None:
            return self._source_journal["generations"][-1]["incarnation"]
        return None

    def _estimate_tokens(self, text: str) -> int:
        return len(text or "")

    def total_tokens(self) -> int:
        return sum(self._estimate_tokens(m.get("content", "")) for m in self.dialog)

    def recent(self) -> list[dict[str, Any]]:
        """Return the window a model sees; the record itself is never trimmed."""

        return self.dialog[-max(2, self.max_rounds * 2):]

    def model_recent(self) -> list[dict[str, Any]]:
        return project_model_history(self._session_id, self.recent())

    def add_user(self, content: str, turn_id: str | None = None, *, message_id: str = "",
                 historical: bool = False):
        return self._add_message("user", content, turn_id, message_id=message_id, historical=historical)

    def add_assistant(self, content: str, turn_id: str | None = None, *, message_id: str = "",
                      historical: bool = False):
        return self._add_message("assistant", content, turn_id, message_id=message_id, historical=historical)

    def _add_message(self, role: str, content: str, turn_id: str | None, *, message_id: str,
                     historical: bool):
        if not content:
            return False
        if message_id:
            prior = next((row for row in self.dialog if row.get("message_id") == message_id), None)
            if prior is not None:
                return (prior.get("role") == role and prior.get("content") == content
                    and prior.get("turn_id") == (str(turn_id) if turn_id else None))
        message = {"role": role, "content": content,
            **new_message_metadata(message_id=message_id, legacy=historical)}
        if turn_id:
            message["turn_id"] = str(turn_id)
        prepared = [message]
        _normalize_message_metadata(prepared)
        self.dialog.append(prepared[0])
        return True

    def mark_last_assistant_interrupted(
        self,
        heard_content: str,
        marker: str = "[interrupted by user]",
        turn_id: str | None = None,
        message_id: str = "",
    ) -> bool:
        """Annotate the latest assistant row of exactly one identified turn.

        Neither another turn's reply nor a row without turn identity can be
        proven interrupted, so a missing or unknown turn id rewrites nothing.
        """
        marker = (marker or "[interrupted by user]").strip()
        heard_content = (heard_content or "").strip()
        target_index = next((index for index in range(len(self.dialog) - 1, -1, -1)
            if turn_id and self.dialog[index].get("role") == "assistant"
            and str(self.dialog[index].get("turn_id") or "") == str(turn_id)
            and (not message_id or self.dialog[index].get("message_id") == message_id)), None)
        if target_index is not None:
            target = self.dialog[target_index]
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
            prepared = [dict(target)]
            _normalize_message_metadata(prepared)
            target = prepared[0]
            target["content"] = content
            target["revision"] += 1
            self.dialog[target_index] = target
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
        for m in self.model_recent():
            messages.append({"role": m["role"], "content": m["content"]})
        # Host-owned facts about this exact turn belong after history and
        # before the user's words. They are neither durable memory nor a
        # rewrite of the user utterance, and therefore cannot be displaced by
        # stale assistant claims in the recent window.
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
        for m in self.model_recent():
            prefix = "ユーザー:" if m["role"] == "user" else "アシスタント:"
            parts.append(f"{prefix}{m['content']}")
        if current_turn_system:
            parts.append(current_turn_system)
        parts.append(f"質問:{latest_user}")
        return "\n\n".join(parts)


def persist_interrupted_assistant_turn(
    session_id: str | None,
    *,
    turn_id: str = "",
    heard_content: str = "",
    marker: str = "[interrupted by user]",
) -> bool:
    """Atomically annotate one interrupted turn in the Session that owns it.

    The caller supplies the visible/playback prefix, already decoded by delivery.
    A Session owns a turn once one of its rows carries that turn id; Cooperative
    ingress records the user row at acceptance, so a reply interrupted before
    final publication gains its assistant row. An unowned turn is never written:
    its runtime kept it elsewhere (AppSession branch, save_history=False) or has
    not committed it yet. Without a turn id no row is provably the interrupted one.
    """
    sid = str(session_id or "").strip()
    tid = str(turn_id or "").strip()
    if not sid or not tid:
        return False
    heard = str(heard_content or "").strip()
    marker = (marker or "[interrupted by user]").strip()
    content = f"{heard} {marker}".strip() if heard else marker
    try:
        history, enable = _prepare_session_update(sid)
        owned = [row for row in history.dialog if row.get("turn_id") == tid]
        if not owned:
            logger.info("Session %r does not own interrupted turn %r; history unchanged", sid, tid)
            return False
        active_reply = _active_assistant_replies.get((sid, tid))
        message_id = active_reply["message_id"] if active_reply else ""
        assistants = [row for row in owned if row.get("role") == "assistant"
            and (not message_id or row.get("message_id") == message_id)]
        if not assistants:
            message = {"role":"assistant", "content":content, "turn_id":tid,
                **(active_reply or new_message_metadata(message_id=assistant_turn_message_id(tid)))}
            history.dialog.append(message)
        elif not history.mark_last_assistant_interrupted(
                heard, marker=marker, turn_id=tid, message_id=message_id):
            return False
        _persist_history(sid, history, enable_conversation=enable)
        if sid == _CURRENT_SESSION_ID:
            conversation_history.dialog = history.dialog
            conversation_history._file_state = history._file_state
        return True
    except Exception:
        logger.exception("failed to record interruption in Session %r", sid)
        return False


# 全局单例
conversation_history = ConversationHistory(summary_token_threshold=3000)

# A delivery lifecycle supplies the missing identity when audio interruption
# precedes its first durable publication. This holds metadata only, not another
# transcript; Session/turn ownership is still checked against committed rows.
_active_assistant_replies: dict[tuple[str, str], dict[str, Any]] = {}
_model_history_projection: Callable | None = None
# One committed Session snapshot, published only by the serial Session owner.
# Source selection never reopens/migrates a file on the conversation loop.
_committed_sources: tuple[str, tuple[dict[str, Any], ...], dict[str, dict[str, Any]], str | None] | None = None
_committed_recent: OrderedDict[str, tuple[dict[str, Any], ...]] = OrderedDict()


def _cache_committed_sources(session_id: str, history: ConversationHistory) -> None:
    global _committed_sources
    window = max(2, history.max_rounds * 2)
    if session_id == _CURRENT_SESSION_ID:
        conversation_history._source_journal = history._source_journal
        prior = (_committed_sources[2] if _committed_sources is not None
            and _committed_sources[0] == session_id else {})
        source_rows = []
        for row in history.dialog:
            previous = prior.get(row["message_id"])
            # Active rows remain mutable. Reuse only an equal detached source;
            # newly committed content must never alias later active edits.
            source_rows.append(previous if previous is not None and previous == row else deepcopy(row))
        rows = tuple(source_rows)
        _committed_sources = (session_id, rows, {row["message_id"]: row for row in rows},
                              history._memory_incarnation())
        recent = rows[-window:]
    else:
        # Inactive Sessions retain only their bounded model window. Copying
        # every durable source here creates no usable source ownership.
        recent = tuple(dict(row) for row in history.dialog[-window:])
    _committed_recent[session_id] = recent
    _committed_recent.move_to_end(session_id)
    while len(_committed_recent) > 16:
        _committed_recent.popitem(last=False)


def committed_session_recent(session_id: str) -> tuple[dict[str, Any], ...]:
    """Bounded previously loaded window; this reader never opens a Session."""
    if session_id not in _committed_recent:
        raise LookupError("Committed Session window is unavailable")
    return tuple(dict(row) for row in _committed_recent[session_id])


def committed_session_rows(session_id: str, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
    snapshot = _committed_sources
    if snapshot is None or snapshot[0] != session_id:
        raise LookupError("Committed Session source snapshot is unavailable")
    return tuple(dict(row) for row in snapshot[1][-limit:])


def read_committed_session_source(session_id: str, message_id: str, *, revision: int) -> dict[str, Any]:
    from core.chat_history_projection import project_memory_source

    snapshot = _committed_sources
    if snapshot is None or snapshot[0] != session_id or message_id not in snapshot[2]:
        raise LookupError("Committed Session source is unavailable")
    return project_memory_source(session_id, snapshot[2][message_id], expected_revision=revision,
                                 incarnation=snapshot[3])


def configure_model_history_projection(projector: Callable | None) -> None:
    """Install the Host's derived source-exclusion projection, never raw edits."""
    global _model_history_projection
    _model_history_projection = projector


def project_model_history(session_id: str | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return (_model_history_projection(session_id, rows)
        if _model_history_projection is not None else [dict(row) for row in rows])


def register_assistant_reply(session_id: str, turn_id: str, metadata: dict[str, Any]) -> None:
    _active_assistant_replies[(session_id, turn_id)] = {
        key:metadata[key] for key in ("message_id", "created_at_ms", "utc_offset_minutes", "revision")}


def release_assistant_reply(session_id: str, turn_id: str, message_id: str) -> None:
    key = (session_id, turn_id)
    if _active_assistant_replies.get(key, {}).get("message_id") == message_id:
        _active_assistant_replies.pop(key)

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
    global _CURRENT_SESSION_ID, _SESSION_SELECTION_REVISION, _committed_sources
    if history is not None:
        # Keep the established singleton object for its existing importers.
        # The model window is Host policy, not Session state, so a Session never
        # carries its own max_rounds into the singleton.
        conversation_history.dialog = history.dialog
        conversation_history.last_summary = history.last_summary
        conversation_history.summary_token_threshold = history.summary_token_threshold
        conversation_history._character_id = history.character_id
        conversation_history._session_id = history._session_id
        conversation_history._file_state = history._file_state
        conversation_history._source_journal = history._source_journal
    _CURRENT_SESSION_ID = session_id
    if session_id is not None and history is not None:
        _cache_committed_sources(session_id, history)
    else:
        _committed_sources = None
    if independent_selection:
        _SESSION_SELECTION_REVISION += 1


def _activate_session(
    session_id: str | None, history: ConversationHistory | None = None,
    *, expected_selection_revision: int | None = None,
) -> None:
    """Publish an already prepared context; failed preparation never gets here."""
    require_session_selection(expected_selection_revision)
    if session_id is not None:
        character_id = history.character_id if history is not None else get_session_character_id(session_id)
        _require_active_character(character_id)
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
        files = [f for f in files if not _source_journal.retired_envelope_hint(os.path.join(_SESSION_DIR, f))]
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
    if _source_journal.enabled():
        _source_journal.recover(_session_path(session_id))
    history = _empty_history()
    history._session_id = session_id
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
    history = conversation_history._persistence_copy()
    try:
        _persist_history(sid, history, enable_conversation=enable_conversation)
        conversation_history.dialog = history.dialog
        conversation_history._file_state = history._file_state
        return True
    except Exception:
        logger.exception("failed to save Session %r", sid)
        return False


def _session_file_signature(path: str) -> tuple[int, ...]:
    status = os.stat(path)
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def _existing_session_file(sid: str, history: ConversationHistory) -> _SessionFileState:
    path = _session_path(sid)
    signature = _session_file_signature(path)
    state = history._file_state
    if state is None or state.path != path or state.signature != signature:
        # External replacement/edits invalidate the header. Preserve ownership
        # validation and title changes without reparsing our own full transcript.
        with open(path, "r", encoding="utf-8") as source:
            data = json.load(source)
        journal = _source_journal.live(data)
        if (state is not None or history._source_journal is not None) and journal != history._source_journal:
            raise ValueError("session_source_journal_changed_outside_owner")
        history._source_journal = journal
        state = _SessionFileState(path, signature, _stored_character_id(data, sid),
            data.get("title"), bool(data.get("enable_conversation", False)))
    if state.character_id != history.character_id:
        raise ValueError("A Session's character identity cannot be changed")
    return state


def _prepare_session_update(sid: str) -> tuple[ConversationHistory, bool]:
    if _source_journal.enabled():
        _source_journal.recover(_session_path(sid))
    if sid != _CURRENT_SESSION_ID:
        return _read_session_history(sid)
    history = conversation_history._persistence_copy()
    history._file_state = _existing_session_file(sid, history)
    return history, history._file_state.enable_conversation


def _persist_history(
    sid: str, history: ConversationHistory, *, enable_conversation: bool,
    create: bool = False, control_sources=(),
) -> None:
    """Persist prepared data without relabelling the currently loaded history."""
    _normalize_message_metadata(history.dialog)
    existing_title = None
    path = _session_path(sid)
    exists = os.path.exists(path)
    if not create and exists:
        existing_title = _existing_session_file(sid, history).title
    if not exists and history._source_journal is not None:
        raise FileNotFoundError("registered_session_missing")
    data = {
        "session_id": sid,
        "character_id": history.character_id,
        "dialog": history.dialog,
        "last_summary": history.last_summary,
        "summary_token_threshold": history.summary_token_threshold,
        "enable_conversation": enable_conversation,
        "timestamp": time.time(),
    }
    if existing_title:
        data["title"] = existing_title
    capture = _source_journal.writes_enabled(sid)
    has_journal = history._source_journal is not None
    has_retired_name = create and exists and _source_journal.retired_envelope_hint(path)
    if capture or has_journal or has_retired_name:
        old = _source_journal.read(path) if exists else None
        prepared = _source_journal.prepare(data, old, create=create, control_sources=control_sources,
                                           capture=capture)
        _source_journal.publish(path, prepared, create=create and not exists)
        history._source_journal = prepared[_source_journal.FIELD]
    else:
        if control_sources:
            raise ValueError("memory_control_source_requires_live_session")
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
        else:
            write_text(path, encoded)
    try:
        history._file_state = _SessionFileState(path, _session_file_signature(path),
            history.character_id, existing_title, enable_conversation)
    except OSError:
        # Replacement already committed. A failed cache refresh cannot turn it
        # into a failed write or leave the committed source view behind.
        history._file_state = None
    _cache_committed_sources(sid, history)


def _validate_stored_history(dialog, summary) -> None:
    if (not isinstance(dialog, list)
            or any(not isinstance(message, dict)
                or not isinstance(message.get('role'), str)
                or not isinstance(message.get('content'), str) for message in dialog)
            or not isinstance(summary, str)):
        raise ValueError('Session history has an invalid shape')


def _read_session_history(session_id: str) -> tuple[ConversationHistory, bool]:
    with open(_session_path(session_id), "r", encoding="utf-8") as source:
        data = json.load(source)
    history, upgraded = _decode_session_history(session_id, data)
    if upgraded:
        # Persist the one-time upgrade before publishing any allocated ids.
        # Preserve the Session timestamp/title: migration is not a new chat.
        data["dialog"] = history.dialog
        write_text(_session_path(session_id), json.dumps(data, ensure_ascii=False, indent=2))
    enable = bool(data.get("enable_conversation", False))
    path = _session_path(session_id)
    history._file_state = _SessionFileState(path, _session_file_signature(path),
        history.character_id, data.get("title"), enable)
    _cache_committed_sources(session_id, history)
    return history, enable


def _decode_session_history(session_id: str, data) -> tuple[ConversationHistory, bool]:
    """Prepare a detached dialog for projection or the normal file migration."""
    _source_journal.live(data)
    character_id = _stored_character_id(data, session_id)
    # Older files also stored max_rounds. The model window is Host policy, so a
    # saved value no longer narrows what the loaded Session sends to a model.
    history = ConversationHistory(
        conversation_history.max_rounds,
        int(data.get("summary_token_threshold", conversation_history.summary_token_threshold)),
        character_id=character_id,
        session_id=session_id,
    )
    history.dialog = data.get("dialog", [])
    history._source_journal = data.get('source_journal')
    history.last_summary = data.get("last_summary", "")
    _validate_stored_history(history.dialog, history.last_summary)
    history.dialog = list(history.dialog)
    return history, _normalize_message_metadata(history.dialog)


def _stored_character_id(data: Any, session_id: str) -> str:
    if not isinstance(data, dict) or data.get("session_id", session_id) != session_id:
        raise ValueError("Session data does not match the requested identity")
    # Only a missing legacy field has the historical default. An explicit bad
    # identity must never be replaced by the startup character.
    return character_prompts.validate_character_id(data.get("character_id", character_prompts.DEFAULT_CHARACTER_ID))


def read_session_sources(session_id: str) -> tuple[dict[str, Any], ...]:
    """Read durable sources on the serial Host owner; legacy reads may migrate."""
    from core.chat_history_projection import project_memory_source

    history, _enable = _read_session_history(session_id)
    incarnation = history._memory_incarnation()
    return tuple(project_memory_source(session_id, row, incarnation=incarnation) for row in history.dialog)


def read_session_source(session_id: str, message_id: str, *, revision: int) -> dict[str, Any]:
    """Resolve an exact revision on the serial Host owner; reads may migrate."""
    history, _enable = _read_session_history(session_id)
    return _project_history_source(session_id, history, message_id, revision=revision)


def _project_history_source(session_id, history, message_id, *, revision):
    from core.chat_history_projection import project_memory_source

    row = next((row for row in history.dialog if row["message_id"] == message_id), None)
    if row is None:
        raise LookupError("Session message source is unavailable")
    return project_memory_source(session_id, row, expected_revision=revision,
                                 incarnation=history._memory_incarnation())


def mark_memory_control_sources(session_id: str, bindings) -> None:
    """Persist already authorized exact control coordinates on the Session owner."""
    history, enable = _prepare_session_update(session_id)
    _persist_history(session_id, history, enable_conversation=enable, control_sources=bindings)
    if session_id == _CURRENT_SESSION_ID:
        conversation_history._file_state = history._file_state
        conversation_history._source_journal = history._source_journal


def get_session_character_id(session_id: str) -> str:
    """Read persisted ownership without activating history or loading a role pack."""
    with open(_session_path(session_id), "r", encoding="utf-8") as source:
        data = json.load(source)
    _source_journal.live(data)
    return _stored_character_id(data, session_id)


class SessionCharacterMismatch(ValueError):
    """A conversation belongs to a different backend startup character."""


def _require_active_character(character_id: str) -> None:
    if character_id != character_prompts.active_character_id():
        raise SessionCharacterMismatch(
            f"This conversation belongs to '{character_id}'. "
            "Select this conversation's character for the next backend startup and restart "
            "the backend to open, continue, rename, or delete it.")


def require_session_character(session_id: str) -> str:
    """Validate chat/role activation before replacing context or retiring a turn."""
    character_id = get_session_character_id(session_id)
    _require_active_character(character_id)
    return character_id


def append_session_message(session_id: str, *, role: str, content: str, turn_id: str,
                           message_id: str = "", created_at_ms: int | None = None,
                           utc_offset_minutes: int | None = None) -> bool:
    """Project a Host-correlated message into its existing Session.

    Called synchronously on the owning Host loop, like Session selection/save.
    This never activates or creates a Session. Replay detection covers the
    Session record; it is not a durable execution/input ledger.
    A turn may publish several messages. Callers with a stable message identity
    retain each part while keeping its original turn correlation.
    """
    if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip() or not turn_id:
        return False
    try:
        content.encode("utf-8", errors="strict")
        history, enable = _prepare_session_update(session_id)
        # An explicit identity is authoritative, never a same-text/turn guess.
        # The legacy no-id API is limited to one row of that role in the turn:
        # it can confirm an unchanged replay, but can neither edit nor add parts.
        prior = next((row for row in history.dialog if (
            row.get("message_id") == message_id if message_id else
            row.get("role") == role and row.get("turn_id") == str(turn_id))), None)
        if prior is not None:
            if not (prior.get("role") == role and prior.get("turn_id") == str(turn_id)
                    and prior.get("content") == content):
                return False
        else:
            message = {"role":role, "content":content, "turn_id":str(turn_id),
                **new_message_metadata(message_id=message_id)}
            if created_at_ms is not None:
                message.update(created_at_ms=created_at_ms, utc_offset_minutes=utc_offset_minutes)
            if re.fullmatch(r"cooperative-role:[0-9a-f]{64}", message["message_id"]):
                return False  # Retired content hashes cannot become new source identities.
            history.dialog.append(message)
        _persist_history(session_id, history, enable_conversation=enable)
        if session_id == _CURRENT_SESSION_ID:
            conversation_history.dialog = history.dialog
            conversation_history._file_state = history._file_state
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
        if _source_journal.enabled():
            _source_journal.recover(_session_path(session_id))
        if not os.path.exists(_session_path(session_id)):
            return False, False
        history, enable = _read_session_history(session_id)
        _activate_session(session_id, history, expected_selection_revision=expected_selection_revision)
        return True, enable
    except Exception:
        logger.exception("failed to load Session %r", session_id)
        return False, False


def delete_session(session_id: str) -> bool:
    global _committed_sources
    try:
        path = _session_path(session_id)
        if _source_journal.enabled():
            _source_journal.recover(path)
        if os.path.exists(path):
            active = session_id == _CURRENT_SESSION_ID
            empty = _empty_history() if active else None
            if active:
                # Fence before removing the active file. If removal then fails,
                # keep the context but do not revive already retired authority.
                _check_activation(None)
            data = _source_journal.read(path)
            capture = _source_journal.writes_enabled(session_id)
            if capture or _source_journal.FIELD in data:
                if not _source_journal.enabled():
                    _source_journal.recover(path)
                if _source_journal.FIELD not in data:
                    data['character_id'] = _stored_character_id(data, session_id)
                    data['dialog'] = data.get('dialog', [])
                    _validate_stored_history(data['dialog'], data.get('last_summary', ''))
                    _normalize_message_metadata(data['dialog'])
                retired = _source_journal.retire(data, capture=capture)
                _source_journal.publish(path, retired)
            else:
                os.remove(path)
            _committed_recent.pop(session_id, None)
            if _committed_sources is not None and _committed_sources[0] == session_id:
                _committed_sources = None
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
    # The retained low-level selection API loads the persisted owner. It cannot
    # manufacture a Session or relabel the currently loaded conversation.
    history = _empty_history() if session_id is None else _read_session_history(session_id)[0]
    _activate_session(session_id, history)


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
        _source_journal.live(data)
        data["title"] = title
        write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        return True
    except Exception as exc:
        logger.error("Session title could not be saved (%s)", type(exc).__name__)
        return False


async def generate_session_title(first_user_message: str) -> str:
    """Derive a local fallback title without contacting an undeclared Provider.

    The current Electron path does not call this compatibility helper, but a
    caller must never upload the first user message to a hard-coded service as
    a side effect of naming a local session.
    """

    compact = " ".join(str(first_user_message or "").split())
    return compact[:30].strip('"\'「」《》 ')
