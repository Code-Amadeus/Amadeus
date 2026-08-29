from __future__ import annotations

import asyncio

from server import host_action_dispatcher as dispatcher


def test_complete_delegate_requires_an_assembled_host() -> None:
    async def run() -> None:
        try:
            dispatcher.record_actions(
                [{"type": "DELEGATE", "attrs": {"intent": "execute", "task": "do it"}}],
                delegate_handler=None,
            )
        except dispatcher.HostDispatchUnavailable:
            return
        raise AssertionError("an unwired Host silently accepted a DELEGATE")

    asyncio.run(run())


def test_incomplete_delegate_is_not_execution_authority() -> None:
    async def run() -> None:
        assert dispatcher.record_actions(
            [{"type": "DELEGATE", "attrs": {"intent": "execute"}}],
            delegate_handler=None,
        ) is None

    asyncio.run(run())


def test_accepted_controls_are_ordered_and_return_a_task_receipt() -> None:
    async def run() -> None:
        seen: list[str] = []

        async def handle(task: str, _attrs: dict) -> None:
            seen.append(task)
            await asyncio.sleep(0)

        receipt = dispatcher.record_actions(
            [
                {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "first"}},
                {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "second"}},
            ],
            delegate_handler=handle,
        )
        assert isinstance(receipt, asyncio.Task)
        await receipt
        assert seen == ["first", "second"]

    asyncio.run(run())


def test_grounded_attrs_do_not_revive_fields_deleted_from_stale_raw() -> None:
    action = {
        "type": "DELEGATE",
        "raw": (
            '[DELEGATE provider="codex" intent="execute" subject="project" '
            'project_id="project-wrong" mode="agent" branch="new" '
            'focus="set" task="old task"]'
        ),
        "attrs": {
            "intent": "amend",
            "subject": "work_item",
            "work_placement": "not_applicable",
            "workspace_ref": "work-right",
            "task": "current task",
            "_host_reference_resolved": True,
            "_host_dispatch_source": "auip_prepare",
        },
    }

    call = dispatcher._delegate_call(action)
    assert call is not None
    task, attrs = call
    assert task == "current task"
    assert attrs["intent"] == "amend"
    assert attrs["workspace_ref"] == "work-right"
    for deleted in ("provider", "project_id", "mode", "branch", "focus"):
        assert deleted not in attrs


def test_raw_only_legacy_delegate_still_has_a_bounded_fallback() -> None:
    call = dispatcher._delegate_call(
        {
            "type": "DELEGATE",
            "raw": '[DELEGATE provider="codex" intent="execute" task="legacy task"]',
        }
    )
    assert call is not None
    task, attrs = call
    assert task == "legacy task"
    assert attrs["provider"] == "codex"
    assert attrs["intent"] == "execute"


def test_explicit_empty_attrs_are_authoritative_over_stale_raw() -> None:
    assert dispatcher._delegate_call(
        {
            "type": "DELEGATE",
            "attrs": {},
            "raw": (
                '[DELEGATE provider="codex" intent="execute" '
                'task="must not revive"]'
            ),
        }
    ) is None


def test_expression_actions_are_forwarded_without_provider_authority() -> None:
    captured: list[dict] = []
    dispatcher.record_actions(
        [{"type": "EMO", "attrs": {"preset": "smile"}}],
        delegate_handler=None,
        expression_sink=lambda actions: captured.extend(actions),
    )
    assert captured == [{"type": "EMO", "attrs": {"preset": "smile"}}]


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")


if __name__ == "__main__":
    _main()
