"""Actual pinned Pi discovery/read/write/resume against a local model endpoint."""
from dataclasses import replace
import json
from pathlib import Path

from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderRunRequest, ProviderSessionHandle
from agent_host.skills import SkillCatalog, configure_skills, provider_skills
from test_extension_skills import clean_catalog as clean_catalog, skill_record
from test_pi_runtime import native_pi as native_pi, pytestmark as pytestmark


def handoff_steps(skill, workspace, output):
    state = {}
    def tool_json(data):
        content = next(m['content'] for m in reversed(data['messages']) if m['role'] == 'tool')
        text = content if isinstance(content, str) else '\n'.join(part.get('text', '') for part in content)
        return json.loads(text)
    def read_facts(data):
        state['layout'] = tool_json(data)
        return {'tool': 'read', 'args': {'path': str(workspace / 'facts.json')}}
    def write_report(data):
        facts = tool_json(data)
        layout = state['layout']
        body = '# ' + layout['heading'] + '\n\n'
        for heading, key in zip(layout['sections'], ('changes', 'verification', 'remaining'), strict=True):
            body += '## ' + heading + '\n' + '\n'.join('- ' + item for item in facts[key]) + '\n\n'
        body += layout['revision_marker'] + '\n'
        return {'tool': 'write', 'args': {'path': str(output), 'content': body}}
    return [{'tool': 'read', 'args': {'path': str(skill.path)}},
            {'tool': 'read', 'args': {'path': str(skill.path.parent / 'references/layout.json')}},
            read_facts, write_report]


async def test_native_pi_skill_is_read_used_restored_replaced_and_removed(native_pi, tmp_path):
    adapter, requests, selected, _ = native_pi
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'facts.json').write_text(json.dumps({'changes': ['Synthetic feature'], 'verification': ['Fixture passed'], 'remaining': ['Review']}))
    first = skill_record(tmp_path / 'installed', providers=('pi',))
    configure_skills(SkillCatalog([first]))
    skill, = provider_skills('pi')
    output = workspace / 'handoff.md'
    # The deterministic model drives real native tools. The instructions and
    # reference must return through those tools, not a test file-copy assertion.
    selected['steps'] = handoff_steps(skill, workspace, output)
    req = ProviderRunRequest('pi', 'Use the available handoff skill. Read facts.json and write handoff.md.', cwd=str(workspace),
        requirements=ProviderRequirements(workspace_access='write', workspace_ownership='caller'))
    events = []

    async def emit(event):
        events.append(event)

    result = await adapter.run(req, 'skill-first', emit)
    assert result.status == 'done', result
    assert 'handoff-layout-v1' in output.read_text()
    assert len([e for e in events if e.type == 'tool.result' and e.payload.get('ok')]) == 4
    assert any('handoff-brief' in str(message) for message in requests[0]['messages'])
    assert any('handoff-layout-v1' in str(message) for message in requests[-1]['messages'])
    session = ProviderSessionHandle.from_dict(json.loads(json.dumps(result.session.to_dict())))
    assert session.skill_bindings

    # The adapter starts a fresh real Pi process on every run; this restores its
    # native JSONL session, including actual skill-read tool results.
    selected['steps'] = []
    resumed = await adapter.run(replace(req, session=session), 'skill-resume', emit)
    assert resumed.status == 'done' and resumed.session == session
    assert any('handoff-layout-v1' in str(message) for message in requests[-1]['messages'])

    second = skill_record(tmp_path / 'installed', providers=('pi',), version='0.2.0', digest='b')
    reference2 = Path(second['package_path']) / 'skills/handoff-brief/references/layout.json'
    text = reference2.read_text(encoding='utf-8')
    if 'handoff-layout-v1' in text:
        reference2.write_text(text.replace('handoff-layout-v1', 'handoff-layout-v2'), encoding='utf-8')
    configure_skills(SkillCatalog([second]))
    count = len(requests)
    blocked = await adapter.run(replace(req, session=session), 'skill-stale', emit)
    assert blocked.status == 'error' and 'Skills changed' in blocked.error
    assert len(requests) == count  # no model request, no old instructions executed
    replacement, = provider_skills('pi')
    selected['steps'] = handoff_steps(replacement, workspace, output)
    updated = await adapter.run(req, 'skill-updated', emit)
    assert updated.status == 'done', updated
    assert 'handoff-layout-v2' in output.read_text()
    assert 'handoff-layout-v1' not in str(requests[-1]['messages'])

    configure_skills(SkillCatalog())
    blocked = await adapter.run(replace(req, session=updated.session), 'skill-removed-resume', emit)
    assert blocked.status == 'error'
    selected['steps'] = []
    removed = await adapter.run(req, 'skill-removed', emit)
    assert removed.status == 'done'
    assert 'handoff-brief' not in str(requests[-1]['messages'])


async def test_pi_skill_reads_do_not_grant_writes_or_neighboring_package_access(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    record = skill_record(tmp_path / 'installed', providers=('pi',))
    configure_skills(SkillCatalog([record]))
    skill, = provider_skills('pi')
    workspace = tmp_path / 'work'
    workspace.mkdir()
    neighbor = Path(record['package_path']) / 'private.txt'
    neighbor.write_text('not a skill resource')
    selected['steps'] = [
        {'tool': 'read', 'args': {'path': str(skill.path)}},
        {'tool': 'read', 'args': {'path': str(neighbor)}},
        {'tool': 'write', 'args': {'path': str(skill.path), 'content': 'overwrite'}},
    ]
    events = []
    async def emit(event):
        events.append(event)
    result = await adapter.run(ProviderRunRequest('pi', 'Inspect the available skill.', cwd=str(workspace),
        requirements=ProviderRequirements(workspace_access='none', workspace_ownership='caller')), 'skill-boundaries', emit)
    assert result.status == 'done', result
    assert [e.payload['ok'] for e in events if e.type == 'tool.result'] == [True, False, False]
    assert skill.path.read_text(encoding='utf-8').startswith('---')


async def test_pi_addition_and_name_collision_do_not_disable_the_agent(native_pi, tmp_path):
    adapter, requests, selected, _ = native_pi
    workspace = tmp_path / 'added-work'
    workspace.mkdir()
    (workspace / 'facts.json').write_text(json.dumps({'changes': ['Added skill'], 'verification': ['Native read'], 'remaining': ['Review']}))
    request = ProviderRunRequest('pi', 'Report capabilities.', cwd=str(workspace),
        requirements=ProviderRequirements(workspace_access='write', workspace_ownership='caller'))
    async def emit(_event):
        pass
    selected['steps'] = []
    before = await adapter.run(request, 'before-addition', emit)
    assert before.status == 'done', before
    duplicate = adapter.agent_dir / 'skills/handoff-brief'
    duplicate.mkdir(parents=True)
    (duplicate / 'SKILL.md').write_text('---\nname: handoff-brief\ndescription: Native unrelated skill\n---\nNative unrelated guidance', encoding='utf-8')
    record = skill_record(tmp_path / 'added-install', providers=('pi',))
    catalog = SkillCatalog([record])
    configure_skills(catalog)
    skill, = provider_skills('pi')
    output = workspace / 'handoff.md'
    selected['steps'] = []
    conflict = await adapter.run(replace(request, session=before.session), 'native-collision', emit)
    assert conflict.status == 'done', conflict
    assert catalog.native_errors
    assert 'Selected installed Skills unavailable' in str(requests[-1]['messages'])
    (duplicate / 'SKILL.md').unlink()
    duplicate.rmdir()
    selected['steps'] = handoff_steps(skill, workspace, output)
    after = await adapter.run(replace(request, session=before.session, task='Use the installed handoff skill.'), 'after-addition', emit)
    assert after.status == 'done', after
    assert after.session.session_id == before.session.session_id
    assert after.session.skill_bindings
    assert 'handoff-layout-v1' in output.read_text()
    assert not catalog.native_errors
