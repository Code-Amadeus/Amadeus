from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_types import ProviderSessionHandle
from agent_host.work_ledger_store import WorkLedgerConflict
from agent_host.work_ledger_types import RunAttemptRecord
from server.provider_session_binding import resolve_provider_session_attachment


def _attempt(*, provider: str = "locus", metadata: dict | None = None) -> RunAttemptRecord:
    return RunAttemptRecord(
        attempt_id="attempt_previous",
        work_item_id="work_item",
        operation_id="operation_previous",
        attempt_number=1,
        provider_run_id="run_previous",
        provider=provider,
        task="Create the first version.",
        mode="agent",
        execution_status="succeeded",
        result="done",
        error="",
        metadata=dict(metadata or {}),
        created_at=time.time(),
        updated_at=time.time(),
        started_at=time.time(),
        finished_at=time.time(),
    )


def _handle(*, provider: str = "locus", scope: str = "work_item") -> dict:
    return ProviderSessionHandle(
        provider=provider,
        session_id="opaque-continuation-handle",
        scope=scope,
    ).to_dict()


def test_attachment_requires_the_declared_capability_and_amendment() -> None:
    previous = _attempt(metadata={"provider_session": _handle()})
    for continuation, resume in (
        ("new", "attach"),
        ("retry", "attach"),
        ("amend", "none"),
        ("amend", "same_attempt"),
    ):
        resolved = resolve_provider_session_attachment(
            has_existing_item=True,
            previous_attempt=previous,
            continuation=continuation,
            provider_capabilities={"resume": resume},
            request_provider="locus",
        )
        assert resolved.session is None
        assert resolved.audit == {}


def test_attachment_uses_the_durable_work_item_scoped_handle() -> None:
    previous = _attempt(metadata={"provider_session": _handle()})
    resolved = resolve_provider_session_attachment(
        has_existing_item=True,
        previous_attempt=previous,
        continuation="amend",
        provider_capabilities={"resume": "attach"},
        request_provider="locus",
    )
    assert resolved.session == ProviderSessionHandle.from_dict(_handle())
    assert resolved.audit == {
        "state": "attached",
        "provider": "locus",
        "previous_attempt_id": previous.attempt_id,
    }


def test_progress_only_retry_may_attach_but_an_ordinary_retry_may_not() -> None:
    previous = _attempt(metadata={"provider_session": _handle()})
    ordinary = resolve_provider_session_attachment(
        has_existing_item=True,
        previous_attempt=previous,
        continuation="retry",
        provider_capabilities={"resume": "attach"},
        request_provider="locus",
    )
    recovered = resolve_provider_session_attachment(
        has_existing_item=True,
        previous_attempt=previous,
        continuation="retry",
        provider_capabilities={"resume": "attach"},
        request_provider="locus",
        recovery_reason="progress_only_completion",
    )

    assert ordinary.session is None
    assert recovered.session == ProviderSessionHandle.from_dict(_handle())
    assert recovered.audit == {
        "state": "attached",
        "provider": "locus",
        "previous_attempt_id": previous.attempt_id,
        "recovery_reason": "progress_only_completion",
    }


def test_attachment_reads_the_canonical_result_fallback() -> None:
    previous = _attempt(
        metadata={"provider_result": {"provider_session": _handle()}}
    )
    resolved = resolve_provider_session_attachment(
        has_existing_item=True,
        previous_attempt=previous,
        continuation="amend",
        provider_capabilities={"resume": "attach"},
        request_provider="locus",
    )
    assert resolved.session is not None
    assert resolved.session.session_id == "opaque-continuation-handle"


def test_attachment_fails_closed_for_invalid_or_cross_provider_state() -> None:
    invalid = _attempt(metadata={"provider_session": {"provider": "locus"}})
    cross_provider = _attempt(
        metadata={"provider_session": _handle(provider="openclaw")}
    )
    for previous in (invalid, cross_provider):
        try:
            resolve_provider_session_attachment(
                has_existing_item=True,
                previous_attempt=previous,
                continuation="amend",
                provider_capabilities={"resume": "attach"},
                request_provider="locus",
            )
        except WorkLedgerConflict:
            pass
        else:
            raise AssertionError("invalid stored session must fail closed")


def test_non_work_item_sessions_do_not_cross_the_ledger_boundary() -> None:
    previous = _attempt(metadata={"provider_session": _handle(scope="attempt")})
    resolved = resolve_provider_session_attachment(
        has_existing_item=True,
        previous_attempt=previous,
        continuation="amend",
        provider_capabilities={"resume": "attach"},
        request_provider="locus",
    )
    assert resolved.session is None
    assert resolved.audit == {}


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all provider session binding tests passed")


if __name__ == "__main__":
    _main()
