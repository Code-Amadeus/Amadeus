"""Generic Needs You requests are bounded, Session-scoped and one-shot."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.attention_request import (
    AttentionOption,
    AttentionRequestCoordinator,
)


async def test_selection_hides_host_ids_and_resolves_once() -> None:
    coordinator = AttentionRequestCoordinator()
    calls: list[str] = []

    async def continuation(option_id: str):
        calls.append(option_id)
        return {"continued": True}

    request = await coordinator.create_selection(
        session_id="session-a",
        title="Choose",
        prompt="Pick one",
        options=[
            AttentionOption(
                option_id="opaque-a",
                label="Chess",
                entity_kind="project",
                metadata={"scope": "persistent", "canonical_id": "must-not-leak"},
            ),
            AttentionOption(
                option_id="opaque-b",
                label="Chess fixes",
                entity_kind="work_item",
                parent_label="Chess",
            ),
        ],
        continuation=continuation,
    )
    assert request["options"][0]["id"] == "opaque-a"
    assert "canonical_id" not in request["options"][0].get("metadata", {})
    assert coordinator.list_pending("session-b") == []

    resolved = await coordinator.resolve(
        session_id="session-a",
        request_id=request["id"],
        option_id="opaque-b",
    )
    assert resolved["ok"] is True
    assert calls == ["opaque-b"]
    duplicate = await coordinator.resolve(
        session_id="session-a",
        request_id=request["id"],
        option_id="opaque-b",
    )
    assert duplicate == {"ok": False, "error": "attention_request_not_found"}
    coordinator.reset_for_tests()


async def test_slow_continuation_cannot_be_claimed_twice() -> None:
    coordinator = AttentionRequestCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def continuation(_option_id: str):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {}

    request = await coordinator.create_selection(
        session_id="session-a",
        title="Choose",
        prompt="Pick one",
        options=[
            AttentionOption(option_id="a", label="A"),
            AttentionOption(option_id="b", label="B"),
        ],
        continuation=continuation,
    )
    first = asyncio.create_task(
        coordinator.resolve(
            session_id="session-a", request_id=request["id"], option_id="a"
        )
    )
    await entered.wait()
    second = await coordinator.resolve(
        session_id="session-a", request_id=request["id"], option_id="b"
    )
    assert second == {"ok": False, "error": "attention_request_not_pending"}
    release.set()
    assert (await first)["ok"] is True
    assert calls == 1
    coordinator.reset_for_tests()


async def test_forged_option_and_session_fail_closed() -> None:
    coordinator = AttentionRequestCoordinator()

    async def continuation(_option_id: str):
        raise AssertionError("invalid resolution must not invoke continuation")

    request = await coordinator.create_selection(
        session_id="session-a",
        title="Choose",
        prompt="Pick one",
        options=[
            AttentionOption(option_id="a", label="A"),
            AttentionOption(option_id="b", label="B"),
        ],
        continuation=continuation,
    )
    wrong_session = await coordinator.resolve(
        session_id="session-b", request_id=request["id"], option_id="a"
    )
    assert wrong_session["error"] == "attention_session_mismatch"
    forged = await coordinator.resolve(
        session_id="session-a", request_id=request["id"], option_id="forged"
    )
    assert forged["error"] == "attention_option_not_found"
    assert len(coordinator.list_pending("session-a")) == 1
    coordinator.reset_for_tests()


async def test_new_exact_control_can_supersede_a_stale_selection() -> None:
    coordinator = AttentionRequestCoordinator()

    async def continuation(_option_id: str):
        raise AssertionError("superseded continuation must not run")

    await coordinator.create_selection(
        session_id="session-a",
        title="Choose",
        prompt="Pick one",
        options=[
            AttentionOption(option_id="a", label="A"),
            AttentionOption(option_id="b", label="B"),
        ],
        continuation=continuation,
        dedupe_key="reference_clarification",
    )
    cancelled = await coordinator.cancel_matching(
        session_id="session-a", dedupe_key="reference_clarification"
    )
    assert cancelled == 1
    assert coordinator.list_pending("session-a") == []
    coordinator.reset_for_tests()


async def test_failed_continuation_is_terminal_and_releases_its_closure() -> None:
    coordinator = AttentionRequestCoordinator()

    async def continuation(_option_id: str):
        raise RuntimeError("simulated continuation failure")

    request = await coordinator.create_selection(
        session_id="session-a",
        title="Choose",
        prompt="Pick one",
        options=[
            AttentionOption(option_id="a", label="A"),
            AttentionOption(option_id="b", label="B"),
        ],
        continuation=continuation,
    )
    failed = await coordinator.resolve(
        session_id="session-a", request_id=request["id"], option_id="a"
    )
    assert failed["error"] == "attention_continuation_failed"
    assert coordinator.list_pending("session-a") == []
    assert request["id"] not in coordinator._requests
    coordinator.reset_for_tests()


async def main() -> None:
    await test_selection_hides_host_ids_and_resolves_once()
    await test_slow_continuation_cannot_be_claimed_twice()
    await test_forged_option_and_session_fail_closed()
    await test_new_exact_control_can_supersede_a_stale_selection()
    await test_failed_continuation_is_terminal_and_releases_its_closure()
    print("all attention request tests passed")


if __name__ == "__main__":
    asyncio.run(main())
