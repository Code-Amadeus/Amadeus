"""Commit versioned memory-source coordinates with the Session that owns them."""

from __future__ import annotations
from copy import deepcopy
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from config.durable_io import _replace, _sync_directory

FIELD = "source_journal"
V1 = "amadeus-message-v1"
V2 = "amadeus-message-v2"
VERSION = "amadeus-memory-text-v1"
logger = logging.getLogger(__name__)

# Optional in-process owner binding. A notification carries no source body or
# permission and is emitted only after the authoritative file was committed.
_publication_observer = None
_publication_owner = None
_publication_scope = None
_publication_formation = None
_publication_capture = True

# The existing Session owner still serializes read/prepare/write. This short
# boundary orders native file installation with formation dispatch/completion.
commit_lock = threading.RLock()


def configure_publication(observer=None, *, owner=None, scope_for_session=None, formation_for_session=None,
                          capture=True):
    global _publication_observer, _publication_owner, _publication_scope, _publication_formation, _publication_capture
    if observer is not None and (not callable(observer) or owner is None):
        raise ValueError("publication_observer_owner_required")
    if observer is not None and _publication_observer is not None:
        raise ValueError("publication_observer_already_bound")
    if observer is None and owner is not _publication_owner:
        raise ValueError("publication_observer_owner_mismatch")
    if scope_for_session is not None and (observer is None or not callable(scope_for_session)):
        raise ValueError("publication_scope_requires_observer")
    if formation_for_session is not None and (observer is None or not callable(formation_for_session)):
        raise ValueError("publication_formation_requires_observer")
    _publication_observer, _publication_owner = observer, owner if observer is not None else None
    _publication_scope = scope_for_session
    _publication_formation = formation_for_session
    _publication_capture = capture


def enabled():
    return _publication_scope is not None or os.environ.get("AMADEUS_SESSION_JOURNAL_ENABLED") == "1"


def scope_fields(user, character, project):
    identifiers = (user, character) if project is None else (user, character, project)
    if any(
        not isinstance(value, str) or not value or value.strip() != value for value in identifiers
    ):
        raise ValueError("journal_explicit_owner_required")
    kind = "character" if project is None else "project"
    return dict(kind=kind, user_id=user, character_id=character, project_id=project)


def binding(character, session_id=None):
    scope_for_session = _publication_scope
    if scope_for_session is not None:
        return scope_for_session(session_id, character)
    user = os.environ.get("AMADEUS_SESSION_JOURNAL_USER", "")
    project = os.environ.get("AMADEUS_SESSION_JOURNAL_PROJECT") or None
    if project is None and os.environ.get("AMADEUS_SESSION_JOURNAL_CHARACTER") != character:
        raise ValueError("journal_explicit_owner_required")
    return scope_fields(user, character, project)


def coordinate(sid, mid, revision, incarnation=None):
    values = [sid, mid, VERSION] if incarnation is None else [sid, incarnation, mid, VERSION]
    return dict(
        namespace=V1 if incarnation is None else V2,
        record_key=json.dumps(values, separators=(",", ":")),
        revision=revision,
        start_cp=None,
        end_cp=None,
    )


def decode_record_key(namespace: str, record_key: str) -> tuple[str, str, str | None, str]:
    values = json.loads(record_key)
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise ValueError("journal_invalid_source_coordinate")
    if namespace == V1 and len(values) == 3:
        sid, mid, version = values
        inc = None
    elif namespace == V2 and len(values) == 4:
        sid, inc, mid, version = values
    else:
        raise ValueError("journal_unknown_source_coordinate")
    return sid, mid, inc, version


def decode(source):
    sid, mid, inc, version = decode_record_key(source.namespace, source.record_key)
    if version != VERSION:
        raise ValueError("journal_projection_unavailable")
    return sid, mid, inc


def validate(data):
    journal = data.get(FIELD)
    if journal is None:
        return None
    if journal.get("version") != 1 or not journal.get("generations"):
        raise ValueError("invalid_journal")
    sid = data["session_id"]
    seen = set()
    for generation in journal["generations"]:
        inc = generation["incarnation"]
        if inc in seen:
            raise ValueError("journal_incarnation_reused")
        seen.add(inc)
        owner = generation["scope"]
        if not isinstance(owner, dict) or owner != scope_fields(
            owner.get("user_id"), data.get("character_id"), owner.get("project_id")
        ):
            raise ValueError("journal_scope_invalid")
        for position, event in enumerate(generation["events"], 1):
            fields = {
                "position",
                "source",
                "action",
                "formation_allowed",
                "policy_revision",
                "recorded_at_ms",
                "lookup_allowed",
                "derived_use_mask",
            }
            if (
                set(event) - {"evidence_only"} != fields
                or event["position"] != position
                or type(event.get("evidence_only", False)) is not bool
            ):
                raise ValueError("journal_prefix_invalid")
            source = event["source"]
            expected = coordinate(
                sid, json.loads(source["record_key"])[-2], source["revision"], inc
            )
            if source != expected or event["action"] not in ("upsert", "withdraw"):
                raise ValueError("journal_event_identity_invalid")
    if any(not generation["retired"] for generation in journal["generations"][:-1]):
        raise ValueError("journal_old_generation_live")
    return journal


def live(data):
    journal = validate(data)
    if journal is not None and journal["generations"][-1]["retired"]:
        raise FileNotFoundError("retired_session")
    return journal


def incarnation(data):
    journal = live(data)
    return journal["generations"][-1]["incarnation"] if journal else None


def _grant(session_id=None):
    if _publication_scope is not None:
        # Only the trusted Host can grant newly committed Session originals.
        # A missing, false or failed decision retains the explicit-lookup grant.
        try:
            formation = (_publication_formation is not None and
                         _publication_formation(session_id) is True)
        except Exception:
            formation = False
        return dict(formation_allowed=formation, policy_revision="host-explicit-lookup-v1",
                    lookup_allowed=True, derived_use_mask=1)
    lookup = os.environ.get("AMADEUS_JOURNAL_LOOKUP_ALLOWED", "1") == "1"
    ordinary = os.environ.get("AMADEUS_JOURNAL_ORDINARY_ALLOWED", "1") == "1"
    derived_lookup = os.environ.get("AMADEUS_JOURNAL_DERIVED_LOOKUP_ALLOWED", "1") == "1"
    formation = os.environ.get("AMADEUS_JOURNAL_FORMATION_ALLOWED", "1") == "1"
    # Host policy generation is independent of its three purpose grants.
    policy = os.environ.get("AMADEUS_JOURNAL_POLICY_REVISION", "g2-journal-grant-v1")
    return dict(
        formation_allowed=formation,
        policy_revision=policy,
        lookup_allowed=lookup,
        derived_use_mask=int(derived_lookup) + 2 * int(ordinary),
    )


def _event(generation, sid, row, action, previous_grant=None):
    grant = (
        _grant(sid)
        if action == "upsert"
        else dict(
            formation_allowed=False,
            policy_revision="g2-journal-grant-v1",
            lookup_allowed=False,
            derived_use_mask=0,
        )
    )
    if previous_grant is not None:
        grant = {field: previous_grant[field] for field in grant}
    return dict(
        position=len(generation["events"]) + 1,
        source=coordinate(sid, row["message_id"], row["revision"], generation["incarnation"]),
        action=action,
        recorded_at_ms=row["created_at_ms"],
        **grant,
    )


def allows_formation(event):
    # The journal keeps the session's base grant independently of each
    # committed message's control purpose. Receiver metadata is effective.
    return event["formation_allowed"] and not event.get("evidence_only", False)


def _bound_controls(sid, generation, previous, current, bindings):
    marked = set()
    for role, reference in bindings:
        values = json.loads(reference["record_key"])
        if (
            not isinstance(values, list)
            or len(values) not in (3, 4)
            or not isinstance(values[-2], str)
        ):
            raise ValueError("memory_control_source_not_current")
        mid = values[-2]
        prior, row = previous.get(mid), current.get(mid)
        if (
            prior is None
            or row is None
            or (prior["role"], prior["revision"]) != (role, row["revision"])
            or row["role"] != role
            or reference != coordinate(sid, mid, row["revision"], generation["incarnation"])
        ):
            raise ValueError("memory_control_source_not_current")
        marked.add(mid)
    return marked


def _latest_events(generation):
    return {json.loads(event["source"]["record_key"])[-2]: event for event in generation["events"]}


def _current_upsert(events, row):
    event = events.get(row["message_id"])
    if (
        event is not None
        and event["action"] == "upsert"
        and event["source"]["revision"] == row["revision"]
    ):
        return event
    return None


def prepare(data, old, *, create=False, control_sources=(), capture=True):
    """Record new grants when enrolled and maintain already committed grants."""
    journal = deepcopy(validate(old)) if old is not None else None
    if control_sources and (journal is None or create):
        raise ValueError("memory_control_source_requires_live_session")
    previous_generation = journal["generations"][-1] if journal is not None else None
    if (
        create
        and old is not None
        and (previous_generation is None or not previous_generation["retired"])
    ):
        raise FileExistsError("session_exists")
    if journal is None:
        if not capture:
            raise ValueError("journal_activation_required")
        journal = dict(version=1, history_start="current_snapshot_only", generations=[])
    sid = data["session_id"]
    scope = binding(data["character_id"], sid) if capture else None
    if previous_generation is None or create:
        incarnation = uuid4().hex if previous_generation else None
        scope = scope if capture else previous_generation["scope"]
        stream_id = f"m0:{sid}:inc:{incarnation}" if incarnation is not None else f"m0:{sid}"
        generation = dict(
            incarnation=incarnation, scope=scope, stream_id=stream_id, retired=False, events=[]
        )
        journal["generations"].append(generation)
        # Launching with capture enabled grants newly committed rows. Existing
        # dialogs require a separate, explicit import.
        old_rows = ({row["message_id"]: row for row in old.get("dialog", [])}
                    if _publication_scope is not None and old is not None and not create else {})
    else:
        generation = previous_generation
        if generation["retired"]:
            raise FileNotFoundError("retired_session")
        if capture and generation["scope"] != scope:
            if _publication_scope is None:
                raise PermissionError("journal_live_scope_rebound")
            if generation["events"]:
                capture = False
            else:
                # A new conversation can pick its Work project before its first message.
                generation["scope"] = scope
        old_rows = {row["message_id"]: row for row in old.get("dialog", [])}
    current = {row["message_id"]: row for row in data["dialog"]}
    controls = _bound_controls(sid, generation, old_rows, current, control_sources)
    latest = _latest_events(generation)
    for mid, row in old_rows.items():
        if mid not in current or (not capture and current[mid]["revision"] != row["revision"]):
            active = _current_upsert(latest, row)
            if active is not None:
                event = _event(generation, sid, row, "withdraw")
                if active.get("evidence_only"):
                    event["evidence_only"] = True
                generation["events"].append(event)
    for mid, row in current.items():
        prior = old_rows.get(mid)
        unchanged = prior is not None and row["revision"] == prior["revision"]
        if prior is not None and row["revision"] < prior["revision"]:
            raise ValueError("journal_revision_rollback")
        if unchanged and (row["role"], row["content"]) != (prior["role"], prior["content"]):
            raise ValueError("journal_body_requires_revision")
        previous = latest.get(mid)
        if (
            prior is None
            and previous is not None
            and row["revision"] <= previous["source"]["revision"]
        ):
            raise ValueError("journal_revision_rollback")
        # An unchanged version authored during suspension has no live grant.
        # A later unrelated save cannot backfill it.
        if unchanged and _current_upsert(latest, row) is None:
            continue
        if capture:
            grant = None
            if _publication_scope is not None and previous and previous["action"] == "upsert":
                grant = previous
            event = _event(generation, sid, row, "upsert", previous_grant=grant)
        elif unchanged and mid in controls:
            event = {**previous, "position": len(generation["events"]) + 1}
        else:
            continue
        if mid in controls or (previous and previous.get("evidence_only")):
            event["evidence_only"] = True
        if previous is None or any(
            value != previous.get(field) for field, value in event.items() if field != "position"
        ):
            generation["events"].append(event)
    value = {**data, FIELD: journal}
    validate(value)
    return value


def retire(data, *, capture=True):
    journal = deepcopy(live(data))
    if journal is None:
        journal = prepare(data, data if _publication_scope is not None else None, capture=capture)[FIELD]
    generation = journal["generations"][-1]
    if capture and _publication_scope is None and generation["scope"] != binding(data["character_id"]):
        raise PermissionError("journal_delete_scope_mismatch")
    latest = _latest_events(generation)
    for row in data.get("dialog", []):
        if _current_upsert(latest, row) is not None:
            generation["events"].append(_event(generation, data["session_id"], row, "withdraw"))
    generation["retired"] = True
    # Reserved envelope key first makes retirement cheaply recognizable to UI
    # discovery without reading a dialog or the event list. Strict readers
    # still validate the full journal; this prefix never authorizes withdrawal.
    value = dict(
        source_journal=journal, session_id=data["session_id"], character_id=data["character_id"]
    )
    validate(value)
    return value


def publish(path, data, *, create=False, previous=None):
    # Observation does not replace the existing synchronous Session mutation
    # owner. In particular it does not pretend a publish-only lock serializes
    # the preceding read/prepare/write operation or a second process writer.
    size = _publish(path, data, create=create)
    observer = _publication_observer
    if observer is not None:
        try:
            generations = data.get(FIELD, {}).get("generations", ())
            if not generations:
                observer(Path(path))
            else:
                current = generations[-1]
                prior = (previous or {}).get(FIELD, {}).get("generations", ())
                offset = (len(prior[-1]["events"]) if prior and
                          prior[-1]["incarnation"] == current["incarnation"] else 0)
                # Only coordinates already committed to the owning file cross this boundary.
                changes = tuple((event["source"]["namespace"], event["source"]["record_key"], event["position"])
                                for event in current["events"][offset:])
                observer(Path(path), generation=(data["session_id"], current["incarnation"]),
                         retired=current["retired"], changes=changes)
        except Exception as exc:
            # The authority file is already durable. Notification failure may
            # delay rebuildable work, never change the committed write result.
            logger.warning("[Session] publication observer failed (%s)", type(exc).__name__)
    return size


def _publish(path, data, *, create=False):
    path = Path(path)
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.journal-{uuid4().hex}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Serialization, staging and fsync occur before this short boundary.
        with commit_lock:
            if create:
                # Windows rename and POSIX link both fail if the name is held.
                install = os.rename if os.name == "nt" else os.link
                install(temporary, path)
            else:
                _replace(temporary, path)
        try:
            _sync_directory(path.parent)
        except OSError:
            logger.warning("[Session] journal directory sync failed after publication")
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("[Session] journal temporary file cleanup failed")
    return len(raw)


def recover(path):
    """Only the serialized M0 owner invokes this before activation/mutation.

    The reserved per-Session names are never authority. No background reader
    calls this; a replacement Host must first own the former writer's lane.
    """
    path = Path(path)
    pattern = re.compile(
        re.escape("." + path.stem + ".journal-") + r"[0-9a-f]{32}-[a-z0-9_]{8}\.tmp\Z"
    )
    removed = []
    for name in os.listdir(path.parent):
        if pattern.fullmatch(name):
            target = path.parent / name
            if target.is_symlink() or not target.is_file():
                continue
            target.unlink()
            removed.append(name)
    return removed


def retired_envelope_hint(path):
    """Best-effort metadata-prefix probe, never a content/permission reader.

    Recognizes both the new first-key envelope and the earlier prototype's
    sid/character/journal envelope. Unknown or malformed files retain names.
    It stops at the dialog key and never decodes its value/event list.
    """
    try:
        with open(path, "rb") as stream:
            remaining = 2048

            def take():
                nonlocal remaining
                remaining -= 1
                if remaining < 0:
                    raise ValueError("prefix_limit")
                return stream.read(1)

            def token():
                value = take()
                while value and value.isspace():
                    value = take()
                return value

            def string():
                if token() != b'"':
                    raise ValueError("string_expected")
                raw = b'"'
                escaped = False
                while True:
                    value = take()
                    if not value:
                        raise ValueError("truncated_prefix")
                    raw += value
                    if value == b'"' and not escaped:
                        return json.loads(raw)
                    escaped = value == b"\\" and not escaped

            if token() != b"{":
                return False
            first = string()
            if first == FIELD:
                return True
            if first not in ("session_id", "character_id") or token() != b":":
                return False
            string()
            if token() != b",":
                return False
            second = string()
            if {first, second} != {"session_id", "character_id"} or token() != b":":
                return False
            string()
            if token() != b",":
                return False
            return string() == FIELD
    except (OSError, ValueError, UnicodeError):
        return False


def read(path):
    with open(path, encoding="utf8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("journal_session_object_required")
    if FIELD not in value and "session_id" not in value:
        value["session_id"] = Path(path).stem
    if value.get("session_id") != Path(path).stem:
        raise ValueError("journal_session_filename_mismatch")
    validate(value)
    return value


def page(path, stream_id, access, *, after, limit):
    if not enabled():
        raise PermissionError("journal_delivery_disabled")
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid_journal_page")
    data = read(path)
    journal = validate(data)
    generation = (
        next((g for g in journal["generations"] if g["stream_id"] == stream_id), None)
        if journal
        else None
    )
    if generation is None:
        raise LookupError("journal_stream_unavailable")
    expected = scope_fields(access.user_id, access.character_id, access.project_id)
    if generation["scope"] != expected:
        raise PermissionError("journal_stream_scope_mismatch")
    if after > len(generation["events"]):
        raise ValueError("journal_receiver_ahead")
    return dict(
        scope=deepcopy(generation["scope"]),
        events=deepcopy(generation["events"][after : after + limit]),
        high_water=len(generation["events"]),
        retired=generation["retired"],
    )


# The launcher owns trial enrollment and the selected AccessScope.
session_scope = None


def writes_enabled(session_id):
    return enabled() and _publication_capture and (session_scope is None or session_scope(session_id))
