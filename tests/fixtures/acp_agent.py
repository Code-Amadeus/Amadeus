"""Real SDK/stdio peer for deterministic Provider boundary tests (no LLM)."""

import asyncio
import json
import os
import sys
from pathlib import Path

from acp import run_agent, text_block, start_tool_call, update_tool_call, RequestError
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    SessionCapabilities,
    SessionResumeCapabilities,
    SessionCloseCapabilities,
    ResumeSessionResponse,
    CloseSessionResponse,
    SetSessionConfigOptionResponse,
    AgentMessageChunk,
    PermissionOption,
    ToolCallUpdate,
)

mode = sys.argv[1]
record = Path(sys.argv[2])


def log(kind, **values):
    with record.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"kind": kind, **values}) + "\n")


OPTIONS = [
    {
        "id": "model",
        "name": "Model",
        "type": "select",
        "category": "model",
        "currentValue": "small",
        "options": [{"value": "small", "name": "Small"}, {"value": "large", "name": "Large"}],
    }
]


class Agent:
    def __init__(self):
        self.stopped = asyncio.Event()

    def on_connect(self, conn):
        self.conn = conn

    async def initialize(self, **kwargs):
        log(
            "initialize",
            capabilities=kwargs.get("client_capabilities").model_dump(by_alias=True),
            pid=os.getpid(),
        )
        if mode == "slow_setup":
            await asyncio.sleep(0.2)
        return InitializeResponse(
            protocol_version=2 if mode == "v2" else 1,
            agent_info=Implementation(name="test-agent", version="1"),
            agent_capabilities=AgentCapabilities(
                session_capabilities=SessionCapabilities(
                    resume=SessionResumeCapabilities(), close=SessionCloseCapabilities()
                )
                if mode != "no_resume"
                else None
            ),
        )

    async def new_session(self, cwd, mcp_servers, **kwargs):
        log("new", cwd=cwd, mcp=[item.model_dump(by_alias=True) for item in mcp_servers])
        return NewSessionResponse(session_id="native-session", config_options=OPTIONS)

    async def resume_session(self, session_id, cwd, **kwargs):
        log("resume", session_id=session_id, cwd=cwd)
        return ResumeSessionResponse(config_options=OPTIONS)

    async def close_session(self, session_id, **kwargs):
        log("close", session_id=session_id)
        return CloseSessionResponse()

    async def set_config_option(self, session_id, config_id, value, **kwargs):
        log("config", config_id=config_id, value=value)
        return SetSessionConfigOptionResponse(
            config_options=[{**OPTIONS[0], "currentValue": value}]
        )

    async def prompt(self, session_id, prompt, **kwargs):
        log(
            "prompt",
            text="".join(item.text for item in prompt),
            secret=os.environ.get("ACP_TEST_SECRET"),
            unrelated=os.environ.get("ACP_UNRELATED_SECRET"),
        )
        if mode == "disconnect":
            os._exit(23)
        if mode == "auth_required":
            raise RequestError.auth_required({"secret": "must-not-appear-in-Host-errors"})
        if mode == "malformed_update":
            # Deliberately invalid native input; bypass only the test agent's
            # serializer to exercise the client's real validation boundary.
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {"sessionUpdate": "tool_call", "title": "Missing id"},
                        },
                    }
                ),
                flush=True,
            )
        if mode == "progress":
            await self.conn.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=text_block("[PROGRESS:DESIGN] Check the requested file."),
                ),
            )
        if mode in {"permission", "permission_wait", "durable_only"}:
            outcome = await self.conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(
                    tool_call_id="tool-1", title="Write the requested file", kind="edit"
                ),
                options=[
                    PermissionOption(
                        option_id="yes",
                        name="Allow",
                        kind="allow_always" if mode == "durable_only" else "allow_once",
                    ),
                    PermissionOption(option_id="no", name="Deny", kind="reject_once"),
                ],
            )
            log("permission", outcome=outcome.model_dump(by_alias=True))
            if outcome.outcome.outcome == "cancelled":
                return PromptResponse(stop_reason="cancelled")
        if mode in {"wait", "ignore_cancel", "slow_setup", "wait_after_update"}:
            if mode == "wait_after_update":
                await self.conn.session_update(
                    session_id=session_id,
                    update=AgentMessageChunk(
                        session_update="agent_message_chunk", content=text_block("Working.")
                    ),
                )
            await self.stopped.wait()
        if self.stopped.is_set():
            return PromptResponse(stop_reason="cancelled")
        await self.conn.session_update(
            session_id=session_id,
            update=start_tool_call("tool-1", "Read file", kind="read", status="in_progress"),
        )
        await self.conn.session_update(
            session_id=session_id,
            update=update_tool_call("tool-1", status="completed", raw_output={"text": "observed"}),
        )
        await self.conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk", content=text_block("Verified result.")
            ),
        )
        return PromptResponse(stop_reason="max_tokens" if mode == "limit" else "end_turn")

    async def cancel(self, session_id, **kwargs):
        log("cancel", session_id=session_id)
        if mode != "ignore_cancel":
            self.stopped.set()


asyncio.run(run_agent(Agent(), use_unstable_protocol=True))
