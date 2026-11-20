"""Native discovery is an explicit launch integration, separate from ACP v1."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from agent_host.acp_configuration import AcpAgentSpec
from agent_host.acp_skills import skill_launch_arguments
from agent_host.skills import SkillCatalog, configure_skills, provider_skills
from test_extension_skills import skill_record


@pytest.fixture(autouse=True)
def clear_skills():
    configure_skills(SkillCatalog())
    yield
    configure_skills(SkillCatalog())


def test_native_discovery_is_explicit_and_overlay_lives_only_for_launch(tmp_path):
    record = skill_record(tmp_path, providers=('custom_acp',))
    configure_skills(SkillCatalog([record]))
    generic = AcpAgentSpec.from_dict({'id': 'deepseek', 'command': sys.executable})
    with skill_launch_arguments(generic) as args:
        assert args == generic.args
    spec = AcpAgentSpec.from_dict({'id': 'custom_acp', 'command': sys.executable,
        'args': ['--patch', 'user.yml'], 'skill_discovery': 'dsh'})
    assert AcpAgentSpec.from_dict(spec.public_dict()) == spec
    with skill_launch_arguments(spec) as args:
        assert args[:2] == ('--patch', 'user.yml')
        patch = Path(args[-1])
        config = json.loads(patch.read_text())[0]['config']
        assert config == {'includeDefaultRoots': False,
            'customSkillDirs': [str(provider_skills('custom_acp')[0].path.parent)]}
    assert not patch.exists()
    with pytest.raises(RuntimeError):
        with skill_launch_arguments(spec) as args:
            failed_patch = Path(args[-1])
            raise RuntimeError('failed startup')
    assert not failed_patch.exists()
    with pytest.raises(ValueError, match='skill_discovery'):
        AcpAgentSpec.from_dict({'id': 'test', 'command': sys.executable, 'skill_discovery': 'guess'})


def test_actual_dsh_discovery_loads_bound_resource_and_removes_it(tmp_path):
    runtime = Path(__file__).resolve().parents[1] / 'build/deepseek-acp-live/runtime/node_modules/@deepseek-ai/dsh-skill-filesystem/lib/index.js'
    if not runtime.exists() or not shutil.which('node'):
        pytest.skip('Optional installed DSH 0.2.0-rc.2 runtime is unavailable')
    record = skill_record(tmp_path, providers=('test_acp',))
    sibling = Path(record['package_path']) / 'skills/not-selected'
    sibling.mkdir()
    (sibling / 'SKILL.md').write_text('---\nname: not-selected\ndescription: Unselected sibling\n---\nprivate sibling', encoding='utf-8')
    spec = AcpAgentSpec('test_acp', 'Test', 'node', skill_discovery='dsh')
    script = '''
import { readFile } from 'node:fs/promises';
import { FileSystemSkillProvider } from __RUNTIME__;
const config = JSON.parse(await readFile(process.argv[1], 'utf8'))[0].config;
const provider = new FileSystemSkillProvider({ get: () => undefined, logger: {warn() {}} },
  { signal: new AbortController().signal, invalidate() {} }, {...config, watch:false});
try {
  const listed = await provider.list({cwd:process.argv[2]});
  const candidates = Array.isArray(listed) ? listed : listed.candidates;
  const loaded = await Promise.all(candidates.map(c => provider.get(c, {})));
  console.log(JSON.stringify(loaded));
} finally { await provider.dispose(); }
'''.replace('__RUNTIME__', json.dumps(runtime.as_uri()))
    for records, expected in (([record], ['handoff-brief']), ([], [])):
        configure_skills(SkillCatalog(records))
        with skill_launch_arguments(spec) as args:
            result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script,
                args[-1], str(tmp_path)], capture_output=True, text=True, encoding='utf-8', timeout=20)
            assert result.returncode == 0, result.stderr
            loaded = json.loads(result.stdout)
        assert [s['name'] for s in loaded] == expected
        if loaded:
            skill = loaded[0]
            assert Path(skill['path']).resolve() == provider_skills('test_acp')[0].path
            assert 'references/layout.json' in skill['content']
            assert (Path(skill['resourceBase']['path']) / 'references/layout.json').is_file()


@pytest.mark.asyncio
async def test_sdk_process_receives_overlay_and_cleanup_runs(tmp_path):
    from test_acp_provider import adapter, records, run_provider
    provider = adapter(tmp_path, config={'skill_discovery': 'dsh'})
    result = await run_provider(provider, tmp_path, [])
    assert result.status == 'done', result.error
    launch = next(row for row in records(tmp_path) if row['kind'] == 'skill_launch')
    assert launch['config'][0]['config']['customSkillDirs'] == []
    assert not Path(launch['path']).exists()
