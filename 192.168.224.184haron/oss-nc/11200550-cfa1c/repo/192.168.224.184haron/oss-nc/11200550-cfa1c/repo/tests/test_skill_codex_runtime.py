"""Real Codex + Codex ACP processes, synthetic loopback model, actual tools.

No account credentials or external inference. The model fixture obtains the
report marker from native tool output; it does not know the reference content
in advance. This checks integration and lifecycle, not LLM selection quality.
"""
from dataclasses import replace
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import time

from aiohttp import web
from openai_codex import CodexConfig
import pytest
import psutil

from agent_host.acp_configuration import AcpAgentSpec
from agent_host.adapters.acp import AcpProviderAdapter
from agent_host.adapters.codex_app_server import CodexAppServerAdapter, _ApprovalAwareAsyncCodex
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderRunRequest, ProviderSessionHandle
from agent_host.skills import SkillCatalog, configure_skills, provider_skills
from config.subprocess_environment import isolated_child_environment
from test_extension_skills import ROOT, clean_catalog as clean_catalog, skill_record

ACP_CLI = ROOT / 'build/skill-acp-runtime/node_modules/@agentclientprotocol/codex-acp/dist/index.js'


def quoted(path):
    return "'" + str(path).replace("'", "''") + "'"


def output_json(data, required_key):
    """Decode JSON inside the actual native exec tool result envelope."""
    for item in reversed(data['input']):
        if item.get('type') != 'function_call_output':
            continue
        text = str(item.get('output', ''))
        for index, char in enumerate(text):
            if char != '{':
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(text[index:])
                if isinstance(value, dict) and required_key in value:
                    return value
            except ValueError:
                pass
    raise AssertionError('The native tool did not return the requested Skill resource')


@pytest.fixture(params=['codex', 'codex_acp'])
async def native_skill_agent(request, tmp_path, monkeypatch):
    provider = request.param
    codex_bin = shutil.which('codex')
    if not codex_bin or os.name != 'nt':
        pytest.skip('Windows Codex native tool fixture requires installed Codex')
    if provider == 'codex_acp' and not ACP_CLI.is_file():
        pytest.skip('Install the pinned temporary @agentclientprotocol/codex-acp 2.2.2 test runtime')
    home = tmp_path / 'codex-home'
    home.mkdir()
    workspace = tmp_path / 'work'
    workspace.mkdir()
    input_file = workspace / 'facts.json'
    input_file.write_text(json.dumps({'changes': ['Synthetic feature'], 'verification': ['Fixture check passed'], 'remaining': ['Review']}))
    output = workspace / 'handoff.md'
    state = {'step': None, 'skill': None, 'requests': [], 'layout': None, 'processes': {}}

    async def responses(http_request):
        data = await http_request.json()
        state['requests'].append(data)
        sequence = len(state['requests'])
        step = state['step']
        command = None
        if step == 0:
            command = f'Get-Content -Raw -LiteralPath {quoted(state["skill"].path)}'
        elif step == 1:
            assert 'references/layout.json' in str(data['input']), 'Skill body never reached the model'
            command = f'Get-Content -Raw -LiteralPath {quoted(state["skill"].path.parent / "references/layout.json")}'
        elif step == 2:
            state['layout'] = output_json(data, 'revision_marker')
            command = f'Get-Content -Raw -LiteralPath {quoted(input_file)}'
        elif step == 3:
            facts = output_json(data, 'changes')
            layout = state['layout']
            body = '# ' + layout['heading'] + '\n\n'
            for section, key in zip(layout['sections'], ('changes', 'verification', 'remaining'), strict=True):
                body += '## ' + section + '\n' + '\n'.join('- ' + item for item in facts[key]) + '\n\n'
            body += layout['revision_marker'] + '\n'
            encoded = base64.b64encode(body.encode()).decode()
            command = f'[IO.File]::WriteAllBytes({quoted(output)}, [Convert]::FromBase64String(\'{encoded}\'))'
        elif step == 4:
            command = f'Set-Content -LiteralPath {quoted(state["skill"].path.parent / "write-probe.txt")} -Value forbidden'
        elif step == 5:
            assert not (state['skill'].path.parent / 'write-probe.txt').exists(), 'Skill became writable outside the workspace'
            last_output = next(item['output'] for item in reversed(data['input']) if item.get('type') == 'function_call_output')
            assert 'denied' in str(last_output).lower() or 'blocked' in str(last_output).lower(), last_output
        if step is not None:
            state['step'] += 1
        if command:
            item = {'type': 'function_call', 'id': f'fc{sequence}', 'call_id': f'call{sequence}', 'name': 'exec_command',
                    'arguments': json.dumps({'cmd': command, 'workdir': str(workspace), 'yield_time_ms': 1000})}
        else:
            item = {'type': 'message', 'id': f'm{sequence}', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'Native fixture completed.', 'annotations': []}]}
        response = {'id': f'resp{sequence}', 'object': 'response', 'status': 'completed', 'output': [item],
                    'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
        events = [
            {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': response},
        ]
        return web.Response(text=''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events),
                            content_type='text/event-stream')

    app = web.Application()
    app.router.add_post('/v1/responses', responses)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1'
    settings = {'model': 'fixture', 'model_provider': 'fixture', 'model_providers.fixture.name': 'Local fixture',
        'model_providers.fixture.base_url': url, 'model_providers.fixture.wire_api': 'responses',
        'model_providers.fixture.env_key': 'FIXTURE_KEY', 'model_providers.fixture.requires_openai_auth': False,
        'model_providers.fixture.supports_websockets': False, 'check_for_update_on_startup': False,
        'windows.sandbox': 'mxc'}
    env = isolated_child_environment(extra={'CODEX_HOME': str(home), 'FIXTURE_KEY': 'synthetic-only',
                                            'TEMP': str(workspace), 'TMP': str(workspace)})
    clients = []

    def factory():
        if provider == 'codex':
            config = CodexConfig(codex_bin=codex_bin, env=env,
                config_overrides=tuple(key + '=' + json.dumps(value) for key, value in settings.items()))
            client = _ApprovalAwareAsyncCodex(config, lambda *args: {'decision': 'accept'})
            clients.append(client)
            adapter = CodexAppServerAdapter(codex=client, model='fixture', model_provider='fixture', auth_mode='model_api',
                sync_desktop_provider=False, approval_mode='deny_all', turn_timeout_s=35, mcp_connections=())
            return adapter
        # Codex ACP reads its normal configuration from this isolated home.
        (home / 'config.toml').write_text('\n'.join(key + '=' + json.dumps(value) for key, value in settings.items()) + '\n', encoding='utf-8')
        extra = {**env, 'CODEX_PATH': codex_bin, 'MODEL_PROVIDER': 'fixture', 'INITIAL_AGENT_MODE': 'workspace-write', 'NO_BROWSER': '1'}
        names = {}
        for key, value in extra.items():
            host_key = 'SKILL_FIXTURE_' + key.upper()
            monkeypatch.setenv(host_key, value)
            names[key] = host_key
        spec = AcpAgentSpec(provider, 'Temporary Codex ACP', shutil.which('node'), args=(str(ACP_CLI),),
                            enabled=True, resume=True, environment=names)
        return AcpProviderAdapter(spec, mcp_connections=(), setup_timeout=20, run_timeout=40)

    try:
        yield factory, state, workspace, output, provider, clients
    finally:
        for client in clients:
            await client.close()
        await runner.cleanup()
        def alive():
            remaining = []
            for pid, created in state['processes'].items():
                try:
                    if psutil.Process(pid).create_time() == created:
                        remaining.append(pid)
                except psutil.NoSuchProcess:
                    pass
            return remaining
        deadline = time.monotonic() + 5
        while alive() and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        remaining = alive()
        for pid in remaining:
            psutil.Process(pid).kill()
        assert not remaining, 'Native Skill test left Agent descendants running'


async def test_native_skill_discovery_use_restart_update_and_uninstall(native_skill_agent, tmp_path):
    factory, state, workspace, output, provider, clients = native_skill_agent
    first = skill_record(tmp_path / 'installed', providers=(provider,))
    configure_skills(SkillCatalog([first]))
    state['skill'], = provider_skills(provider)
    state['step'] = 0
    request = ProviderRunRequest(provider, 'Use the available handoff skill. Read facts.json and write handoff.md.',
        cwd=str(workspace), requirements=ProviderRequirements(workspace_access='write', workspace_ownership='caller'))
    events = []
    async def emit(event):
        events.append(event)
        for process in psutil.Process().children(recursive=True):
            try:
                state['processes'][process.pid] = process.create_time()
            except psutil.NoSuchProcess:
                pass
    adapter = factory()
    first_result = await adapter.run(request, 'native-first', emit)
    assert first_result.status == 'done', first_result
    assert 'handoff-layout-v1' in output.read_text(encoding='utf-8')
    assert 'Synthetic feature' in output.read_text(encoding='utf-8')
    assert 'handoff-brief' in json.dumps(state['requests'][0])
    assert len([e for e in events if e.type == 'tool.result' and e.payload.get('ok', True)]) >= 4
    saved = ProviderSessionHandle.from_dict(json.loads(json.dumps(first_result.session.to_dict())))
    assert saved.skill_bindings
    await adapter.close()

    for client in clients:
        await client.close()

    adapter = factory()  # new native process, same durable native session home
    state['step'] = None
    resumed = await adapter.run(replace(request, session=saved), 'native-resume', emit)
    assert resumed.status == 'done' and resumed.session == saved, resumed
    assert 'handoff-layout-v1' in json.dumps(state['requests'][-1])

    second = skill_record(tmp_path / 'installed', providers=(provider,), version='0.2.0', digest='b')
    reference = Path(second['package_path']) / 'skills/handoff-brief/references/layout.json'
    text = reference.read_text(encoding='utf-8')
    if 'handoff-layout-v1' in text:
        reference.write_text(text.replace('handoff-layout-v1', 'handoff-layout-v2'), encoding='utf-8')
    configure_skills(SkillCatalog([second]))
    count = len(state['requests'])
    stale = await adapter.run(replace(request, session=saved), 'native-stale', emit)
    assert stale.status == 'error' and 'Skills changed' in stale.error
    assert len(state['requests']) == count
    await adapter.close()
    for client in clients:
        await client.close()
    adapter = factory()
    state['skill'], = provider_skills(provider)
    state['step'] = 0
    updated = await adapter.run(request, 'native-updated', emit)
    assert updated.status == 'done', updated
    assert 'handoff-layout-v2' in output.read_text(encoding='utf-8')
    assert 'handoff-layout-v1' not in json.dumps(state['requests'][-1])
    configure_skills(SkillCatalog())
    removed = await adapter.run(replace(request, session=updated.session), 'native-removed', emit)
    assert removed.status == 'error'
    await adapter.close()
    for client in clients:
        await client.close()
    adapter = factory()
    state['step'] = None
    fresh = await adapter.run(replace(request, task='Report available capabilities.'), 'native-fresh', emit)
    assert fresh.status == 'done', fresh
    assert 'handoff-brief' not in json.dumps(state['requests'][-1])
    await adapter.close()


async def test_native_session_created_before_skills_can_resume_and_use_new_skill(native_skill_agent, tmp_path):
    factory, state, workspace, output, provider, _clients = native_skill_agent
    adapter = factory()
    async def emit(_event):
        pass
    request = ProviderRunRequest(provider, 'Report available capabilities.', cwd=str(workspace),
        requirements=ProviderRequirements(workspace_access='write', workspace_ownership='caller'))
    try:
        empty = await adapter.run(request, 'before-skills', emit)
        assert empty.status == 'done', empty
        assert not empty.session.skill_bindings and not empty.session.skill_revision
        record = skill_record(tmp_path / 'new-install', providers=(provider,))
        configure_skills(SkillCatalog([record]))
        state['skill'], = provider_skills(provider)
        state['step'] = 0
        result = await adapter.run(replace(request, task='Use the handoff skill with facts.json.', session=empty.session), 'after-addition', emit)
        assert result.status == 'done', result
        assert result.session.session_id == empty.session.session_id
        assert result.session.skill_bindings
        assert 'handoff-layout-v1' in output.read_text(encoding='utf-8')
    finally:
        await adapter.close()
