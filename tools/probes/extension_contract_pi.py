"""Controlled local model driving the real Pi owner; no account or remote service."""

from dataclasses import replace
import json
from pathlib import Path
from aiohttp import web


async def read_skill_with_pi(record, scenario, base, mark):
    from agent_host.adapters.pi import PiAdapter
    from agent_host.skills import SkillCatalog, configure_skills
    from agent_host.provider_contract import ProviderRequirements
    from agent_host.provider_types import ProviderRunRequest

    if scenario["provider"] != "pi" or "pi" not in record["providers"]:
        raise ValueError("This native check requires an existing Pi binding")
    catalog = SkillCatalog([record])
    skills = catalog.for_provider("pi")
    if catalog.errors or not skills:
        raise ValueError("Skill catalog rejected the selected package")
    reads = []
    package = Path(record["package_path"]).resolve()
    for relative in scenario["reads"]:
        target = (package / relative).resolve()
        if not any(target.is_relative_to(skill.path.parent.resolve()) for skill in skills):
            raise ValueError("Requested read is outside the selected Skill resources")
        reads.append(str(target))
    if not reads or not all(str(skill.path.resolve()) in reads for skill in skills):
        raise ValueError("Read the selected SKILL.md before its resources")
    pending = list(reads)
    calls = []

    async def completions(request):
        data = await request.json()
        calls.append(data)
        delta = {"role": "assistant", "content": "Controlled native Skill check complete."}
        if pending:
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{len(calls)}",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"path": pending.pop(0)}),
                        },
                    }
                ],
            }
        frames = [
            {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls" if "tool_calls" in delta else "stop",
                    }
                ],
            },
        ]
        return web.Response(
            text="".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n",
            content_type="text/event-stream",
        )

    service = web.Application()
    service.router.add_post("/v1/chat/completions", completions)
    runner = web.AppRunner(service)
    await runner.setup()
    adapter = None
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        agent = base / "pi"
        agent.mkdir()
        (agent / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "fixture": {
                            "baseUrl": f"http://127.0.0.1:{port}/v1",
                            "api": "openai-completions",
                            "apiKey": "local-fixture",
                            "models": [{"id": "fixture", "reasoning": False, "input": ["text"]}],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        configure_skills(catalog)
        adapter = PiAdapter(
            agent_dir=agent,
            model_provider="fixture",
            model="fixture",
            timeout=25,
            mcp_connections=(),
        )
        mark("skill.native_discovery_and_read")
        adapter.require_startup_ready()
        events = []

        async def emit(event):
            events.append(event)

        request = ProviderRunRequest(
            "pi",
            "Read the selected Skill and supplied resources.",
            cwd=str(base),
            requirements=ProviderRequirements(
                workspace_access="none", workspace_ownership="caller"
            ),
        )
        result = await adapter.run(request, "author-skill-read", emit)
        successful = [
            event for event in events if event.type == "tool.result" and event.payload.get("ok")
        ]
        if (
            result.status != "done"
            or len(successful) != len(reads)
            or catalog.native_errors
            or not result.session.skill_bindings
        ):
            raise RuntimeError("Native Skill discovery/read failed")
        mark("skill.native_resume")
        restored = await adapter.run(
            replace(request, session=result.session), "author-skill-resume", emit
        )
        if restored.status != "done" or restored.session != result.session:
            raise RuntimeError("Native Skill session did not resume unchanged")
        return {
            "provider": "pi",
            "reads": len(reads),
            "resumed": True,
            "model": "controlled local fixture; selection quality not tested",
        }
    finally:
        if adapter:
            await adapter.close()
        configure_skills(SkillCatalog())
        await runner.cleanup()
