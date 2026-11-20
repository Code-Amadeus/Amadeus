"""Skill package scope, metadata, and durable native-session compatibility."""
import copy
import json
import os
from pathlib import Path
import shutil

import pytest

from agent_host.provider_types import ProviderSessionHandle
from agent_host.skills import SkillCatalog, configure_skills, provider_skills, skill_revision, skill_bindings, skill_session_error, with_skill_catalog, record_native_skill_discovery
from server.extensions.package import validate_installed
from server.extensions.runtime import ExtensionRuntime

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'examples/extensions/handoff-skill/src/amadeus_example_handoff'


def skill_record(tmp_path, *, providers=('codex', 'pi', 'acp_test'), version='0.1.0', digest='a'):
    if installed := os.environ.get('AMADEUS_TEST_SKILL_SNAPSHOTS'):
        record = json.loads((Path(installed) / f'skill-{version}.json').read_text(encoding='utf-8'))
        record['providers'] = list(providers)
        return record
    package = tmp_path / version / SOURCE.name
    shutil.copytree(SOURCE, package)
    manifest = json.loads((package / 'amadeus-extension.json').read_text(encoding='utf-8'))
    manifest['version'] = version
    (package / 'amadeus-extension.json').write_text(json.dumps(manifest), encoding='utf-8')
    return {'id': manifest['id'], 'version': version, 'digest': digest * 64, 'manifest': manifest,
            'enabled': True, 'package_path': str(package), 'data_path': str(tmp_path / 'data'),
            'config': {}, 'secrets': {}, 'providers': list(providers)}


@pytest.fixture(autouse=True)
def clean_catalog():
    configure_skills(SkillCatalog())
    yield
    configure_skills(SkillCatalog())


def test_installed_skill_has_discovery_metadata_without_instruction_body(tmp_path):
    record = skill_record(tmp_path, providers=('codex',))
    validate_installed(record['manifest'], Path(record['package_path']))
    configure_skills(SkillCatalog([record]))
    prompt = with_skill_catalog('Prepare handoff', 'codex')
    assert 'handoff-brief' in prompt and 'SKILL.md' in prompt
    assert 'Read `references/layout.json`' not in prompt
    assert provider_skills('pi') == ()
    assert with_skill_catalog('unchanged', 'pi') == 'unchanged'


@pytest.mark.parametrize('edit', ['name', 'description', 'frontmatter', 'link'])
def test_invalid_skill_fails_before_agent_execution(tmp_path, edit):
    record = skill_record(tmp_path)
    path = Path(record['package_path']) / 'skills/handoff-brief/SKILL.md'
    text = path.read_text(encoding='utf-8')
    if edit == 'link':
        path.unlink()
        try:
            path.symlink_to(SOURCE / 'skills/handoff-brief/SKILL.md')
        except OSError:
            pytest.skip('symlink privilege unavailable')
    else:
        text = text.replace('name: handoff-brief', 'name: different') if edit == 'name' else text.replace('description:', 'missing:') if edit == 'description' else 'No frontmatter'
        path.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        validate_installed(record['manifest'], Path(record['package_path']))


def test_name_conflicts_only_block_overlapping_agent_bindings(tmp_path):
    first = skill_record(tmp_path, providers=('codex',))
    second = copy.deepcopy(first)
    second['id'] = 'com.other.handoff'
    second['providers'] = ['pi']
    catalog = SkillCatalog([first, second])
    assert not catalog.errors and len(catalog.skills) == 2
    second['providers'] = ['codex']
    catalog = SkillCatalog([first, second])
    assert set(catalog.errors) == {first['id'], second['id']} and not catalog.skills


@pytest.mark.parametrize('change', ['update', 'remove', 'unbind'])
def test_saved_session_resumes_only_the_same_selected_skill_revision(tmp_path, change):
    record = skill_record(tmp_path)
    configure_skills(SkillCatalog([record]))
    session = ProviderSessionHandle('codex', 'native-session', 'interaction', skill_bindings=skill_bindings('codex'))
    saved = json.loads(json.dumps(session.to_dict()))
    restored = ProviderSessionHandle.from_dict(saved)
    configure_skills(SkillCatalog([record]))  # Host restart
    assert not skill_session_error('codex', restored)
    if change == 'update':
        record.update(version='0.2.0', digest='b' * 64)
    elif change == 'remove':
        record['enabled'] = False
    else:
        record['providers'] = ['pi']
    configure_skills(SkillCatalog([record]))
    assert 'Start a new Agent session' in skill_session_error('codex', restored)
    assert not skill_session_error('codex', None)


def test_runtime_skill_catalog_is_agent_bound_and_cleared_on_close(tmp_path):
    record = skill_record(tmp_path)
    runtime = ExtensionRuntime([record])
    try:
        runtime.start()
        assert provider_skills('pi')
        contribution, = runtime.capability_packages()[0].contributions
        assert contribution.kind == 'skill'
        assert contribution.bindings[0].projection == 'agent_skill'
        assert not contribution.supports_surface('main_chat')
    finally:
        runtime.close()
    assert provider_skills('pi') == ()


def test_reinstall_is_a_new_session_revision_but_other_agent_bindings_are_independent(tmp_path):
    original = skill_record(tmp_path / 'first', providers=('codex',))
    configure_skills(SkillCatalog([original]))
    session = ProviderSessionHandle('codex', 'native', skill_revision=skill_revision('codex'))
    other = skill_record(tmp_path / 'other', providers=('pi',))
    configure_skills(SkillCatalog([original, other]))
    assert not skill_session_error('codex', session)
    reinstalled = skill_record(tmp_path / 'reinstalled', providers=('codex',))
    configure_skills(SkillCatalog([reinstalled, other]))
    assert skill_session_error('codex', session)


def test_skill_additions_preserve_existing_and_pre_skill_sessions(tmp_path):
    empty = ProviderSessionHandle('codex', 'old-without-skills')
    record = skill_record(tmp_path / 'first', providers=('codex',))
    configure_skills(SkillCatalog([record]))
    assert not skill_session_error('codex', empty)
    saved = ProviderSessionHandle('codex', 'has-first', skill_bindings=skill_bindings('codex'))
    second = skill_record(tmp_path / 'second', providers=('codex',))
    second['id'] = 'com.example.second'
    original = Path(second['package_path']) / 'skills/handoff-brief'
    renamed = original.with_name('second-skill')
    original.rename(renamed)
    skill_file = renamed / 'SKILL.md'
    skill_file.write_text(skill_file.read_text().replace('name: handoff-brief', 'name: second-skill'))
    second['manifest']['contributes']['skill'][0]['path'] = 'skills/second-skill'
    configure_skills(SkillCatalog([record, second]))
    assert len(provider_skills('codex')) == 2
    restored = ProviderSessionHandle.from_dict(json.loads(json.dumps(saved.to_dict())))
    assert not skill_session_error('codex', restored)
    assert not skill_session_error('codex', empty)
    record['version'] = '0.2.0'
    configure_skills(SkillCatalog([record, second]))
    assert skill_session_error('codex', restored)


def test_native_discovery_uses_exact_paths_and_reports_per_agent_health(tmp_path):
    from types import SimpleNamespace
    record = skill_record(tmp_path, providers=('codex', 'pi'))
    runtime = ExtensionRuntime([record], provider_manifest_lookup=lambda _: SimpleNamespace(
        capabilities=SimpleNamespace(capability_projections=('agent_skill',))))
    try:
        runtime.start()
        skill, = provider_skills('codex')
        other = str(tmp_path / 'global/handoff-brief/SKILL.md')
        assert not record_native_skill_discovery('codex', [other, str(skill.path)])
        assert record_native_skill_discovery('pi', [other])
        status = runtime.statuses()[0]['contributions'][0]
        assert status['health'] == 'degraded'
        assert {t['id']: t['state'] for t in status['provider_targets']} == {'codex': 'ready', 'pi': 'error'}
        assert not record_native_skill_discovery('pi', [str(skill.path)])
        assert runtime.statuses()[0]['contributions'][0]['health'] == 'ready'
    finally:
        runtime.close()
