"""Answer presentation only; exact clocks and evidence remain with their owners."""
from datetime import datetime, timedelta, timezone


def admitted_time_metadata(records, pins, view):
    """Use only pins and Session rows already admitted/verified by this view."""
    from core import session_source_journal as journal

    offsets, instants = {}, {}
    for row, pin in zip(records, pins, strict=True):
        sid, mid, _ = journal.decode(pin.source)
        # No view lookup/read: require_reads has checked this exact snapshot.
        source = view.files[sid][4][mid]
        assert (source["revision"], source["created_at_ms"]) == (
            pin.source.revision, pin.recorded_at_ms)
        identifier = row["source_id"]
        offsets[identifier] = source.get("utc_offset_minutes")
        if row["recorded_time"]["status"] == "known":
            instants.setdefault(pin.recorded_at_ms, []).append(identifier)
    return {"offsets": offsets, "recorded_order": [instants[at] for at in sorted(instants)]}


def compact_clock(clock, offset=None, *, precision="minute"):
    """Render a Host UTC clock at its originating offset, or explicitly UTC."""
    if precision not in {"minute", "day"}:
        raise ValueError("invalid clock presentation precision")
    if clock is None or clock.get("status") != "known":
        return None
    value = datetime.fromisoformat(clock["utc"].replace("Z", "+00:00"))
    assert value.utcoffset() == timedelta(0)
    if offset is None:
        zone = "Z"
    else:
        assert type(offset) is int and -1440 < offset < 1440
        value = value.astimezone(timezone(timedelta(minutes=offset)))
        hours, minutes = divmod(abs(offset), 60)
        zone = f"{'+' if offset >= 0 else '-'}{hours:02}:{minutes:02}"
    return value.strftime("%Y-%m-%d %H:%M" if precision == "minute" else "%Y-%m-%d") + zone


def answer_time_view(event, metadata, *, precision="minute"):
    """Copy only explicit mixed lookup frames; never alter the canonical event."""
    if (event.get("state") != "memory_lookup" or event.get("saved_memories_only", True)
            or metadata is None):
        return event
    payload = event["memory_reference"]
    records = [{**row, "recorded_time": compact_clock(row["recorded_time"],
                metadata["offsets"][row["source_id"]], precision=precision)} for row in payload["records"]]
    reference = {**payload, "records": records, "recorded_order": metadata["recorded_order"]}
    question = event.get("question_time")
    return {**event, "memory_reference": reference,
            "question_time": compact_clock(question, (question or {}).get("utc_offset_minutes"), precision=precision)}
