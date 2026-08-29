from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.adapters.openclaw import OpenClawAdapter
from agent_host.provider_identity import MAIN_ROLE_NAME_METADATA_KEY
from agent_host.provider_types import (
    ProviderEvent,
    ProviderRunRequest,
    ProviderSessionHandle,
    ProviderSteerRequest,
)
from openclaw.gateway_client import OpenClawGatewayError


class _FakeGatewayClient:
    instances: list["_FakeGatewayClient"] = []
    responses: list[str] = []
    blocked_turns: set[int] = set()
    disconnect_sends: int = 0
    disconnect_waits: int = 0
    reconnect_fails: bool = False

    @classmethod
    def configure(
        cls,
        *responses: str,
        blocked_turns: set[int] | None = None,
        disconnect_sends: int = 0,
        disconnect_waits: int = 0,
        reconnect_fails: bool = False,
    ) -> None:
        cls.instances = []
        cls.responses = list(responses)
        cls.blocked_turns = set(blocked_turns or set())
        cls.disconnect_sends = max(0, int(disconnect_sends))
        cls.disconnect_waits = max(0, int(disconnect_waits))
        cls.reconnect_fails = bool(reconnect_fails)

    def __init__(self, **_kwargs) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.closed = False
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.run_done: dict[str, asyncio.Event] = {}
        self.run_status: dict[str, str] = {}
        self.turn_count = 0
        self.connect_count = 0
        self.disconnected_sends = 0
        self.disconnected_waits = 0
        self.last_assistant = ""
        self.advertised_methods = frozenset(
            {
                "agent.wait",
                "chat.history",
                "sessions.abort",
                "sessions.create",
                "sessions.get",
                "sessions.send",
            }
        )
        type(self).instances.append(self)

    async def connect(self):
        self.connect_count += 1
        if self.connect_count > 1 and type(self).reconnect_fails:
            raise OpenClawGatewayError(
                "gateway still unavailable",
                code="CONNECTION_FAILED",
            )
        return {"features": {"methods": list(self.advertised_methods)}}

    async def close(self) -> None:
        self.closed = True

    async def request(self, method: str, params: dict, *, timeout=None):
        del timeout
        self.requests.append((method, dict(params)))
        if method == "sessions.create":
            return {"ok": True, "key": params["key"]}
        if method == "sessions.get":
            return {
                "messages": [
                    {"role": "user", "content": "previous task"},
                    {"role": "assistant", "content": "previous result"},
                ]
            }
        if method == "sessions.send":
            self.turn_count += 1
            run_id = f"native-turn-{self.turn_count}"
            done = self.run_done.setdefault(run_id, asyncio.Event())
            self.run_status[run_id] = "running"
            if self.turn_count not in type(self).blocked_turns:
                response = (
                    type(self).responses[self.turn_count - 1]
                    if self.turn_count <= len(type(self).responses)
                    else "done"
                )
                self.last_assistant = response
                await self.events.put(
                    {
                        "type": "event",
                        "event": "chat",
                        "payload": {
                            "runId": run_id,
                            "state": "final",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": response}],
                            },
                        },
                    }
                )
                self.run_status[run_id] = "ok"
                done.set()
            if self.disconnected_sends < type(self).disconnect_sends:
                self.disconnected_sends += 1
                raise OpenClawGatewayError(
                    "connection lost before sessions.send acknowledgement",
                    code="CONNECTION_LOST",
                )
            return {"runId": run_id, "status": "started"}
        if method == "agent.wait":
            if self.disconnected_waits < type(self).disconnect_waits:
                self.disconnected_waits += 1
                raise OpenClawGatewayError(
                    "reader failed: keepalive ping timeout",
                    code="CONNECTION_LOST",
                )
            run_id = params["runId"]
            await self.run_done.setdefault(run_id, asyncio.Event()).wait()
            return {"status": self.run_status.get(run_id, "ok")}
        if method == "sessions.abort":
            run_id = params["runId"]
            if self.run_status.get(run_id) != "running":
                return {"abortedRunId": None, "status": "no-active-run"}
            self.run_status[run_id] = "aborted"
            self.run_done.setdefault(run_id, asyncio.Event()).set()
            await self.events.put(
                {
                    "type": "event",
                    "event": "chat",
                    "payload": {"runId": run_id, "state": "aborted"},
                }
            )
            return {"abortedRunId": run_id, "status": "aborted"}
        if method == "chat.history":
            return {
                "messages": [
                    {"role": "assistant", "content": self.last_assistant}
                ]
            }
        raise AssertionError(method)

    async def next_event(self, *, timeout=None):
        try:
            if timeout is None:
                return await self.events.get()
            return await asyncio.wait_for(self.events.get(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise OpenClawGatewayError(
                "event timeout",
                code="EVENT_TIMEOUT",
            ) from exc


def _run(coro):
    return asyncio.run(coro)


def test_openclaw_uses_the_shared_progress_contract_without_leaking_markers() -> None:
    _FakeGatewayClient.configure(
        "Visible opening.\n"
        "[PROGRESS:DESIGN] Use explicit turn ownership to avoid races.\n"
        "[PROGRESS:CAPABILITY] Two players can now alternate turns.\n"
        "Visible middle.\n"
        "[PROGRESS:VALIDATION] 12 tests passed.\n"
        "Visible final."
    )

    async def scenario():
        events: list[ProviderEvent] = []

        async def emit(event: ProviderEvent) -> None:
            events.append(event)

        result = await OpenClawAdapter(
            gateway_client_factory=_FakeGatewayClient,
        ).run(
            ProviderRunRequest(
                provider="openclaw",
                task="Build a two-player counter.",
                metadata={"timeout": 9.0},
            ),
            "openclaw-progress-1",
            emit,
        )
        return result, events

    result, events = _run(scenario())
    sent_message = next(
        params["message"]
        for method, params in _FakeGatewayClient.instances[-1].requests
        if method == "sessions.send"
    )
    assert all(
        marker in sent_message
        for marker in (
            "[PROGRESS:DESIGN]",
            "[PROGRESS:CAPABILITY]",
            "[PROGRESS:VALIDATION]",
        )
    )
    visible = "".join(
        str(event.payload.get("text") or "")
        for event in events
        if event.type == "assistant.delta"
    )
    assert "Visible opening." in visible
    assert "Visible middle." in visible
    assert "Visible final." in visible
    assert "[PROGRESS:" not in visible
    semantic = [event for event in events if event.type == "semantic.progress"]
    assert [event.payload.get("milestone") for event in semantic] == [
        "design",
        "capability",
        "validation",
    ]


def test_openclaw_receives_role_reference_context_without_task_rewrite() -> None:
    _FakeGatewayClient.configure("done")
    task = "你能找到关于你自己的公开资料吗？"

    async def scenario():
        result = await OpenClawAdapter(
            gateway_client_factory=_FakeGatewayClient,
        ).run(
            ProviderRunRequest(
                provider="openclaw",
                task=task,
                metadata={
                    "timeout": 9.0,
                    MAIN_ROLE_NAME_METADATA_KEY: "Makise Kurisu (牧瀬紅莉栖)",
                },
            ),
            "openclaw-identity-1",
            lambda _event: asyncio.sleep(0),
        )
        return result

    result = _run(scenario())
    sent_message = next(
        params["message"]
        for method, params in _FakeGatewayClient.instances[-1].requests
        if method == "sessions.send"
    )
    assert result.status == "done"
    assert task in sent_message
    assert "[Amadeus role-reference context]" in sent_message
    assert 'execution Provider is "openclaw"' in sent_message
    assert result.status == "done"
    assert "[PROGRESS:" not in result.result
    assert result.session is not None and result.session.provider == "openclaw"


def test_openclaw_steer_uses_exact_abort_then_same_session() -> None:
    _FakeGatewayClient.configure(
        "",
        "Revised instruction completed.",
        blocked_turns={1},
    )

    async def scenario():
        events: list[ProviderEvent] = []

        async def emit(event: ProviderEvent) -> None:
            events.append(event)

        adapter = OpenClawAdapter(gateway_client_factory=_FakeGatewayClient)
        task = asyncio.create_task(
            adapter.run(
                ProviderRunRequest(
                    provider="openclaw",
                    task="Open the page and keep working for a while.",
                    metadata={"work": {"work_item_id": "work-web"}},
                ),
                "openclaw-steer-1",
                emit,
            )
        )
        while not _FakeGatewayClient.instances or not any(
            method == "sessions.send"
            for method, _params in _FakeGatewayClient.instances[-1].requests
        ):
            await asyncio.sleep(0)
        outcome = await adapter.steer(
            "openclaw-steer-1",
            ProviderSteerRequest(
                task="Use the same page and report only the title.",
                revision=1,
            ),
        )
        result = await asyncio.wait_for(task, timeout=2.0)
        return adapter, outcome, result, events

    adapter, outcome, result, events = _run(scenario())
    assert outcome["accepted"] is True
    assert outcome["safe_boundary"] == "confirmed_abort_then_same_session"
    client = _FakeGatewayClient.instances[-1]
    sends = [params for method, params in client.requests if method == "sessions.send"]
    assert len(sends) == 2
    assert sends[0]["key"] == sends[1]["key"]
    assert "same page" in sends[1]["message"]
    assert "replaces the unfinished portion" not in sends[0]["message"]
    assert "replaces the unfinished portion" in sends[1]["message"]
    assert "latest instruction as authoritative" in sends[1]["message"]
    assert (
        "sessions.abort",
        {"key": sends[0]["key"], "runId": "native-turn-1"},
    ) in client.requests
    assert result.result == "Revised instruction completed."
    assert result.metadata["native_run_ids"] == ["native-turn-1", "native-turn-2"]
    assert result.metadata["steer_revisions"] == [1]
    assert result.session is not None and result.session.scope == "work_item"
    applied = [
        event
        for event in events
        if event.type == "run.status"
        and event.payload.get("stage") == "steer_applied"
    ]
    assert len(applied) == 1
    assert applied[0].payload == {
        "status": "running",
        "stage": "steer_applied",
        "revision": 1,
        "safe_boundary": "confirmed_abort_then_same_session",
    }
    assert not adapter._controls


def test_openclaw_completed_followup_attaches_the_typed_session() -> None:
    _FakeGatewayClient.configure("The existing page is still available.")
    session = ProviderSessionHandle(
        provider="openclaw",
        session_id="agent:main:dashboard:amadeus-existing",
        scope="work_item",
    )

    async def scenario():
        async def emit(_event: ProviderEvent) -> None:
            return None

        return await OpenClawAdapter(
            gateway_client_factory=_FakeGatewayClient,
        ).run(
            ProviderRunRequest(
                provider="openclaw",
                task="Continue from the existing page.",
                session=session,
            ),
            "openclaw-followup-1",
            emit,
        )

    result = _run(scenario())
    assert result.session == session
    assert result.metadata["session_attached"] is True
    client = _FakeGatewayClient.instances[-1]
    assert ("sessions.get", {"key": session.session_id}) in client.requests
    assert not any(method == "sessions.create" for method, _params in client.requests)
    send = next(params for method, params in client.requests if method == "sessions.send")
    assert send["key"] == session.session_id


def test_openclaw_result_prose_cannot_override_native_completion() -> None:
    _FakeGatewayClient.configure(
        "Observed the requested page; it currently displays a connection-refused error."
    )

    async def scenario():
        async def emit(_event: ProviderEvent) -> None:
            return None

        return await OpenClawAdapter(
            gateway_client_factory=_FakeGatewayClient,
        ).run(
            ProviderRunRequest(provider="openclaw", task="Inspect the page state."),
            "openclaw-observed-error-text-1",
            emit,
        )

    result = _run(scenario())
    assert result.status == "done"
    assert result.metadata["result_type"] == "error"
    assert "connection-refused error" in result.result


def test_openclaw_reconciles_an_accepted_run_without_replaying_it() -> None:
    _FakeGatewayClient.configure(
        "Recovered final result.",
        disconnect_waits=1,
    )

    async def scenario():
        events: list[ProviderEvent] = []

        async def emit(event: ProviderEvent) -> None:
            events.append(event)

        result = await OpenClawAdapter(
            gateway_client_factory=_FakeGatewayClient,
        ).run(
            ProviderRunRequest(provider="openclaw", task="Research one fact."),
            "openclaw-recover-1",
            emit,
        )
        return result, events

    result, events = _run(scenario())
    client = _FakeGatewayClient.instances[-1]
    assert result.status == "done"
    assert result.result == "Recovered final result."
    assert result.metadata["transport_recoveries"] == 1
    assert client.connect_count == 2
    assert sum(method == "sessions.send" for method, _params in client.requests) == 1
    assert sum(method == "agent.wait" for method, _params in client.requests) == 2
    assert any(method == "chat.history" for method, _params in client.requests)
    assert "Recovered final result." in "".join(
        str(event.payload.get("text") or "")
        for event in events
        if event.type == "assistant.delta"
    )


def test_openclaw_preserves_unknown_accepted_run_without_replay() -> None:
    _FakeGatewayClient.configure(
        "Late result.",
        disconnect_waits=1,
        reconnect_fails=True,
    )

    async def scenario():
        async def emit(_event: ProviderEvent) -> None:
            return None

        return await OpenClawAdapter(
            gateway_client_factory=_FakeGatewayClient,
        ).run(
            ProviderRunRequest(provider="openclaw", task="Perform one action."),
            "openclaw-orphan-1",
            emit,
        )

    result = _run(scenario())
    client = _FakeGatewayClient.instances[-1]
    assert result.status == "orphaned"
    assert result.session is not None
    assert result.metadata["runtime_resumable"] is False
    assert result.metadata["outcome_uncertainty"] == "provider_run_may_still_be_active"
    assert "was not replayed" in str(result.error)
    assert sum(method == "sessions.send" for method, _params in client.requests) == 1


def test_openclaw_does_not_replay_when_send_acknowledgement_is_lost() -> None:
    _FakeGatewayClient.configure(
        "Possibly completed result.",
        disconnect_sends=1,
    )

    async def scenario():
        async def emit(_event: ProviderEvent) -> None:
            return None

        return await OpenClawAdapter(
            gateway_client_factory=_FakeGatewayClient,
        ).run(
            ProviderRunRequest(provider="openclaw", task="Perform one action."),
            "openclaw-send-uncertain-1",
            emit,
        )

    result = _run(scenario())
    client = _FakeGatewayClient.instances[-1]
    assert result.status == "orphaned"
    assert result.session is not None
    assert "acceptance" in str(result.error)
    assert sum(method == "sessions.send" for method, _params in client.requests) == 1


def test_openclaw_cancel_reports_only_exact_native_confirmation() -> None:
    _FakeGatewayClient.configure("", blocked_turns={1})

    async def scenario():
        async def emit(_event: ProviderEvent) -> None:
            return None

        adapter = OpenClawAdapter(gateway_client_factory=_FakeGatewayClient)
        task = asyncio.create_task(
            adapter.run(
                ProviderRunRequest(provider="openclaw", task="Wait."),
                "openclaw-cancel-1",
                emit,
            )
        )
        while not _FakeGatewayClient.instances or not any(
            method == "sessions.send"
            for method, _params in _FakeGatewayClient.instances[-1].requests
        ):
            await asyncio.sleep(0)
        outcome = await adapter.cancel("openclaw-cancel-1")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return outcome

    outcome = _run(scenario())
    assert outcome["confirmed"] is True
    assert outcome["cancelled"] is True
    assert outcome["native_run_id"] == "native-turn-1"


if __name__ == "__main__":
    for test in (
        test_openclaw_uses_the_shared_progress_contract_without_leaking_markers,
        test_openclaw_steer_uses_exact_abort_then_same_session,
        test_openclaw_completed_followup_attaches_the_typed_session,
        test_openclaw_result_prose_cannot_override_native_completion,
        test_openclaw_reconciles_an_accepted_run_without_replaying_it,
        test_openclaw_preserves_unknown_accepted_run_without_replay,
        test_openclaw_does_not_replay_when_send_acknowledgement_is_lost,
        test_openclaw_cancel_reports_only_exact_native_confirmation,
    ):
        test()
        print(f"ok: {test.__name__}")
