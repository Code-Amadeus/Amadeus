"""Real runtime ControlDecision shadow keeps evidence and authority separate."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.chat_runtime import ChatRuntime, _TurnState
from server.control_proposal import seal_control_proposals
from server.control_shadow import RuntimeControlDecisionShadow


class _Coordinator:
    def __init__(self, *, complete: bool = True) -> None:
        self.complete = complete

    def workspace_routing_context(self, *, limit: int):
        assert limit == 200
        return {
            "focus": {
                "mode": "pinned",
                "projectId": "project_true",
                "workItemId": "work_true",
            },
            "candidates": [
                {"projectId": "project_true", "projectName": "True Project"}
            ],
            "candidateCount": 1 if self.complete else 2,
            "candidatesComplete": self.complete,
        }

    @staticmethod
    def conversation_work_items_for_resolution(session_id: str, *, limit: int):
        assert session_id == "session-shadow"
        assert limit == 200
        return {
            "items": [
                {
                    "work_item_id": "work_true",
                    "project_id": "project_true",
                    "title": "Edit README",
                    "files": ["README.md"],
                    "state": "active",
                    "execution": "running",
                    "relation": "running",
                }
            ],
            "complete": True,
        }

    @staticmethod
    def conversation_binding(session_id: str):
        assert session_id == "session-shadow"
        return {
            "defaultProjectId": "project_true",
            "projectId": "project_true",
            "workItemId": "work_true",
        }


def _batch():
    return seal_control_proposals(
        [
            {
                "type": "DELEGATE",
                "attrs": {
                    "provider": "locus",
                    "intent": "execute",
                    "project_id": "role_guess",
                    "workspace_ref": "role_work_guess",
                    "task": "edit README",
                },
            }
        ],
        turn_id="turn-shadow",
        session_id="session-shadow",
        user_text="继续修改 True Project 的 README",
        transport="inline_tag",
        prior_messages=(
            {"role": "user", "content": "之前在做哪个项目？"},
            {"role": "assistant", "content": "之前在 True Project。"},
        ),
    )


def test_runtime_shadow_uses_prior_history_and_never_current_role_reply() -> None:
    async def run() -> None:
        asked = []
        evidence = []

        async def query(messages):
            asked.append(messages)
            joined = "\n".join(message["content"] for message in messages)
            if "[Independent candidate verdict - FINAL]" in joined:
                return '{"evidence":"exact"}'
            return (
                '{"decisions":[{"proposal_index":0,"provider":"locus",'
                '"intent":"amend","subject":"work_item",'
                '"work_placement":"not_applicable",'
                '"session_context":"unchanged",'
                '"reference_mode":"candidates"}]}'
            )

        observer = RuntimeControlDecisionShadow(
            coordinator=_Coordinator(),
            query=query,
            sink=evidence.append,
        )
        with (
            patch(
                "server.work_context.augment_system_prompt_for_control_decision",
                side_effect=lambda prompt, **_kwargs: prompt + "\n[ACTIVE FACTS]",
            ),
            patch(
                "llm.prompts.registered_provider_ids",
                return_value=("locus", "browser"),
            ),
        ):
            result = await observer.capture(_batch())

        assert result.outcome == "diverge"
        assert result.raw_controls[0]["intent"] == "execute"
        assert result.raw_controls[0]["project_id"] == "role_guess"
        assert result.raw_references == (
            ("project:role_guess", "work_item:role_work_guess"),
        )
        assert result.canonical_controls[0]["intent"] == "amend"
        assert result.canonical_controls[0]["workspace_ref"] == "work_true"
        assert "project_id" not in result.canonical_controls[0]
        assert result.canonical_references == (("work_item:work_true",),)
        assert result.candidate_count == 2
        assert result.decision_protocol_retries == 0
        assert result.candidate_verdict_queries == 2
        assert result.candidate_protocol_retries == 0
        assert result.candidate_failure_reply == ""
        assert result.exhaustive_candidate_limit == 64
        assert evidence == [result]
        assert len(asked) == 3
        messages = asked[0]
        assert messages[-1]["role"] == "user"
        assert "继续修改 True Project" in messages[-1]["content"]
        joined = "\n".join(str(message.get("content") or "") for message in messages)
        assert "之前在做哪个项目" in joined
        assert "之前在 True Project" in joined
        assert "本轮角色回答不应出现" not in joined
        assert "[ACTIVE FACTS]" in messages[0]["content"]
        assert "project:project_true" not in joined
        assert "work_item:work_true" not in joined
        candidate_frames = [
            "\n".join(message["content"] for message in call)
            for call in asked[1:]
        ]
        assert all("[Independent candidate verdict - FINAL]" in frame for frame in candidate_frames)
        assert sum("project:project_true" in frame for frame in candidate_frames) == 1
        assert sum("work_item:work_true" in frame for frame in candidate_frames) == 1
        assert sum("session_focus=true" in frame for frame in candidate_frames) == 2
        assert sum("session_current=true" in frame for frame in candidate_frames) == 2
        assert sum("relation=running" in frame for frame in candidate_frames) == 1

    asyncio.run(run())


def test_control_prompt_augmentation_excludes_reference_rosters() -> None:
    from server.work_context import augment_system_prompt_for_control_decision

    with (
        patch(
            "server.work_context.render_active_provider_context",
            return_value="active provider fact",
        ),
        patch(
            "server.work_context.render_branch_routing_context",
            return_value="[Active browser branch]\nbranch fact",
        ),
        patch(
            "server.work_context.render_workspace_routing_context",
            return_value="Project identities are withheld",
        ) as project_renderer,
        patch(
            "server.work_context.render_conversation_work_context",
            return_value="WorkItem identities are withheld",
        ) as work_renderer,
    ):
        prompt = augment_system_prompt_for_control_decision(
            "base control contract",
            session_id="session-shadow",
        )

    assert "base control contract" in prompt
    assert "active provider fact" in prompt
    assert "branch fact" in prompt
    assert "Project identities are withheld" in prompt
    assert "WorkItem identities are withheld" in prompt
    assert project_renderer.call_args.kwargs["include_candidates"] is False
    assert work_renderer.call_args.kwargs["include_candidates"] is False


def test_incomplete_project_catalog_fails_closed_without_querying() -> None:
    async def run() -> None:
        calls = 0
        evidence = []

        async def query(_messages):
            nonlocal calls
            calls += 1
            return "{}"

        observer = RuntimeControlDecisionShadow(
            coordinator=_Coordinator(complete=False),
            query=query,
            sink=evidence.append,
        )
        with (
            patch(
                "server.work_context.augment_system_prompt_for_control_decision",
                side_effect=lambda prompt, **_kwargs: prompt,
            ),
            patch("llm.prompts.registered_provider_ids", return_value=("locus",)),
        ):
            result = await observer.capture(_batch())
        assert calls == 0
        assert result.decision_status == "incomplete"
        assert result.outcome == "incomplete"
        assert result.canonical_controls == ()
        assert evidence == [result]

        bounded = RuntimeControlDecisionShadow(
            coordinator=_Coordinator(complete=True),
            query=query,
            sink=evidence.append,
            exhaustive_candidate_limit=1,
        )
        with (
            patch(
                "server.work_context.augment_system_prompt_for_control_decision",
                side_effect=lambda prompt, **_kwargs: prompt,
            ),
            patch("llm.prompts.registered_provider_ids", return_value=("locus",)),
        ):
            bounded_result = await bounded.capture(_batch())
        assert calls == 0
        assert bounded_result.decision_status == "incomplete"
        assert "2>1" in bounded_result.reason
        assert bounded_result.exhaustive_candidate_limit == 1

    asyncio.run(run())


def test_compound_runtime_shadow_is_opt_in_payload_free_and_non_authoritative() -> None:
    async def run() -> None:
        calls = 0
        evidence = []

        async def query(messages):
            nonlocal calls
            calls += 1
            joined = "\n".join(message["content"] for message in messages)
            if "[Compound control decomposition - FINAL]" in joined:
                return '{"clauses":["继续修改 True Project 的 README"]}'
            if "[Independent candidate verdict - FINAL]" in joined:
                return '{"evidence":"exact"}'
            return (
                '{"decisions":[{"proposal_index":0,"provider":"locus",'
                '"intent":"amend","subject":"work_item",'
                '"work_placement":"not_applicable",'
                '"session_context":"unchanged",'
                '"reference_mode":"candidates"}]}'
            )

        disabled = RuntimeControlDecisionShadow(
            coordinator=_Coordinator(),
            query=query,
        )
        assert disabled.capture_compound_shadow(_batch()) is None
        assert calls == 0

        observer = RuntimeControlDecisionShadow(
            coordinator=_Coordinator(),
            query=query,
            compound_shadow=True,
            compound_sink=evidence.append,
        )
        with (
            patch(
                "server.work_context.augment_system_prompt_for_control_decision",
                side_effect=lambda prompt, **_kwargs: prompt,
            ),
            patch("llm.prompts.registered_provider_ids", return_value=("locus",)),
        ):
            result = await observer.capture_compound_shadow(_batch())

        assert result.status == "ok"
        assert len(result.operations) == 1
        assert result.operations[0].action["intent"] == "amend"
        assert result.operations[0].action["workspace_ref"] == "work_true"
        assert evidence == [result]
        record = result.as_log_record()
        assert record["operationCount"] == 1
        assert record["operations"][0]["references"] == ["work_item:work_true"]
        serialized = str(record)
        assert "继续修改" not in serialized
        assert "edit README" not in serialized
        assert calls == 4  # decomposition + decision + two candidate verdicts

    asyncio.run(run())


def test_runtime_handoff_records_role_control_before_host_grounding() -> None:
    async def run() -> None:
        captured = []
        dispatched = []

        class Observer:
            def capture(self, batch):
                captured.append(batch)

                async def done():
                    return None

                return done()

        runtime = ChatRuntime()
        runtime._control_proposal_observer = Observer()
        st = _TurnState(
            gui_callback=None,
            turn_id="turn-ground",
            question="修改 README",
            session_id="session-ground",
            control_prior_messages=(
                {"role": "assistant", "content": "旧的、已完成的角色回答。"},
            ),
        )

        def ground(action, *_args, **_kwargs):
            action["attrs"]["intent"] = "amend"
            action["attrs"]["workspace_ref"] = "work_true"
            return True

        with (
            patch.object(runtime, "_ground_present_provider_delegate", side_effect=ground),
            patch.object(runtime, "_annotate_delegate_source"),
            patch.object(runtime, "_annotate_report_lookup"),
            patch("core.chat_runtime.record_actions", side_effect=lambda actions: dispatched.extend(actions)),
        ):
            runtime._consume_stream_chunk(
                st,
                '[DELEGATE provider="locus" intent="execute" task="edit README"]',
            )
            await asyncio.sleep(0)

        assert captured[0].proposals[0]["intent"] == "execute"
        assert "workspace_ref" not in captured[0].proposals[0]
        assert captured[0].prior_messages[0]["content"].startswith("旧的")
        assert dispatched[0]["attrs"]["intent"] == "amend"
        assert dispatched[0]["attrs"]["workspace_ref"] == "work_true"

    asyncio.run(run())


def test_message_query_preserves_roles_for_the_control_backend() -> None:
    from llm import client

    calls = []

    class Completions:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"decisions":[]}'))]
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    messages = [
        {"role": "system", "content": "control"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "prior"},
        {"role": "user", "content": "current"},
    ]
    with (
        patch.object(client, "LLM_PROVIDER", "deepseek"),
        patch.object(client, "llm_client", fake_client),
    ):
        reply = client.remote_llm_messages_query(messages, temperature=0.0)
    assert reply == '{"decisions":[]}'
    assert calls[0]["messages"] == messages
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["response_format"] == {"type": "json_object"}


if __name__ == "__main__":
    test_runtime_shadow_uses_prior_history_and_never_current_role_reply()
    print("ok: runtime shadow uses only prior history and current user")
    test_control_prompt_augmentation_excludes_reference_rosters()
    print("ok: control prompt excludes Project and WorkItem rosters")
    test_incomplete_project_catalog_fails_closed_without_querying()
    print("ok: incomplete Project catalog does not query")
    test_compound_runtime_shadow_is_opt_in_payload_free_and_non_authoritative()
    print("ok: compound runtime shadow is opt-in and payload-free")
    test_runtime_handoff_records_role_control_before_host_grounding()
    print("ok: raw role control stays distinct from host grounding")
    test_message_query_preserves_roles_for_the_control_backend()
    print("ok: control backend preserves message roles")
