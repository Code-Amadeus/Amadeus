"""Contract tests for the deliberately thin narration delivery boundary."""

from __future__ import annotations

import asyncio
from dataclasses import fields

from server.narration_delivery import NarrationRequest, deliver_narration


def _request(*, source: str = "work", payload: dict | None = None) -> NarrationRequest:
    return NarrationRequest(
        request_id=f"request-{source}",
        source_kind=source,
        source_id=f"source-{source}",
        session_id="session-1",
        payload=payload or {"display_text": "done"},
    )


def test_delivery_forwards_opaque_payload_and_preserves_identity() -> None:
    async def run() -> None:
        payload = {
            "display_text": "done",
            "terminal": True,
            "narration_keypoint": "must_remain_opaque",
        }
        seen: list[dict] = []

        async def sink(value: dict) -> dict:
            seen.append(value)
            return {
                "status": "queued",
                "sentence_id": "sentence-1",
                "last_sentence_id": "sentence-1",
            }

        receipt = await deliver_narration(_request(payload=payload), sink)

        assert seen == [
            {
                **payload,
                "_narration_delivery": {
                    "source_kind": "work",
                    "source_id": "source-work",
                    "session_id": "session-1",
                    "request_id": "request-work",
                },
            }
        ]
        assert receipt.accepted is True
        assert receipt.to_dict() == {
            "request_id": "request-work",
            "source_kind": "work",
            "source_id": "source-work",
            "status": "queued",
            "accepted": True,
            "sentence_id": "sentence-1",
            "last_sentence_id": "sentence-1",
        }

    asyncio.run(run())


def test_delivery_does_not_retry_or_convert_a_drop_into_success() -> None:
    async def run() -> None:
        calls = 0

        async def sink(_value: dict) -> dict:
            nonlocal calls
            calls += 1
            return {"status": "dropped", "reason": "queue_full"}

        receipt = await deliver_narration(_request(source="auip"), sink)

        assert calls == 1
        assert receipt.accepted is False
        assert receipt.status == "dropped"
        assert receipt.to_dict()["reason"] == "queue_full"

    asyncio.run(run())


def test_delivery_reports_legacy_unknown_and_sink_failure_without_policy() -> None:
    async def run() -> None:
        legacy = await deliver_narration(_request(source="vn"), lambda _payload: None)
        unknown = await deliver_narration(
            _request(source="host"),
            lambda _payload: {"status": "future_status", "reason": "new"},
        )

        def fail(_payload: dict) -> dict:
            raise RuntimeError("boom")

        failed = await deliver_narration(_request(source="auip"), fail)

        assert legacy.status == "queued_legacy_sink"
        assert legacy.accepted is False
        assert unknown.status == "unknown"
        assert unknown.to_dict()["sink_status"] == "future_status"
        assert failed.status == "error"
        assert failed.to_dict()["reason"] == "sink_failed:RuntimeError"

    asyncio.run(run())


def test_request_rejects_missing_identity_and_unknown_sources() -> None:
    for values in (
        {"request_id": "", "source_kind": "work", "source_id": "source"},
        {"request_id": "request", "source_kind": "unknown", "source_id": "source"},
        {"request_id": "request", "source_kind": "work", "source_id": ""},
    ):
        try:
            NarrationRequest(**values)
        except ValueError:
            continue
        raise AssertionError(f"invalid request was accepted: {values}")


def test_request_shape_cannot_grow_into_a_second_governor_silently() -> None:
    assert [item.name for item in fields(NarrationRequest)] == [
        "request_id",
        "source_kind",
        "source_id",
        "session_id",
        "payload",
    ]


if __name__ == "__main__":
    test_delivery_forwards_opaque_payload_and_preserves_identity()
    test_delivery_does_not_retry_or_convert_a_drop_into_success()
    test_delivery_reports_legacy_unknown_and_sink_failure_without_policy()
    test_request_rejects_missing_identity_and_unknown_sources()
    test_request_shape_cannot_grow_into_a_second_governor_silently()
    print("ok: narration delivery remains a source-neutral receipt boundary")
