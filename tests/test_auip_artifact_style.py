"""Generation preference and CSS delivery boundaries, without model calls."""
import asyncio
from pathlib import Path
import shutil

import pytest

from agent_host.provider_authoring import stage_auip_authoring_bundle
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerStore
from config import settings
from server.handlers.system_handler import _artifact_configuration
from server.work_ledger_coordinator import WorkLedgerCoordinator
from tools.validate_auip_entry import validate_entry
from test_auip_entry_preflight import _write_fixture

BASE = Path(__file__).resolve().parents[1] / 'skills/auip-authoring/assets/amadeus-v1.css'


@pytest.mark.parametrize('enabled', [True, False])
def test_preference_reaches_staged_provider_resources_without_restyling(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(settings, 'AUIP_ARTIFACT_STYLE_ENABLED', enabled)
    field = _artifact_configuration(settings)[0]['fields'][0]
    assert field['value'] is enabled and field['restart_required'] is True
    workspace = tmp_path / 'app'
    workspace.mkdir()
    existing = workspace / 'index.html'
    existing.write_text('<p>Existing user design</p>', encoding='utf-8')

    async def scenario():
        with WorkLedgerStore(tmp_path / 'ledger.sqlite3') as store:
            coordinator = WorkLedgerCoordinator(store)
            try:
                request = coordinator.prepare_request(ProviderRunRequest(
                    provider='codex', task='Integrate the existing application with AUIP.',
                    cwd=str(workspace), mode='agent', metadata={
                        'source':'auip_prepare',
                        'host_outcome_requirement': {'operation':'prepare','facet':'auip.application',
                            'expected':{'current_attempt_contribution':True}},
                        'provider_manifest':CODEX_APP_SERVER_MANIFEST.to_dict(),
                    }))
                skill = Path(request.metadata['auip_authoring_skill_path'])
                asset = skill.parent / 'assets/amadeus-v1.css'
                guide = skill.parent / 'references/artifact-style.md'
                assert asset.is_file() is enabled
                assert guide.is_file() is enabled
                if enabled:
                    assert asset.read_bytes() == BASE.read_bytes()
                assert existing.read_text(encoding='utf-8') == '<p>Existing user design</p>'
                # Staging inputs does not inject a stylesheet into the application.
                assert not (workspace / 'styles/amadeus-v1.css').exists()
                return skill
            finally:
                coordinator.close()
    staged = asyncio.run(scenario())
    before = staged.read_bytes()
    monkeypatch.setattr(settings, 'AUIP_ARTIFACT_STYLE_ENABLED', not enabled)
    assert staged.read_bytes() == before  # Already dispatched guidance stays frozen.


def test_disabled_staging_preserves_plain_authoring_bundle(tmp_path):
    skill = stage_auip_authoring_bundle(tmp_path, include_artifact_style=False)
    source = BASE.parents[1] / 'SKILL.md'
    assert skill.read_bytes() == source.read_bytes()
    assert not (skill.parent / 'assets/amadeus-v1.css').exists()


@pytest.mark.parametrize('case', ['applied', 'missing', 'unused', 'custom', 'import_missing', 'outside', 'disabled', 'wide'])
def test_real_entry_css_delivery_and_advisory_boundaries(tmp_path, case):
    manifest, entry = _write_fixture(tmp_path / 'bundle', option_available=True)
    root = entry.parent
    styles = root / 'styles'
    styles.mkdir()
    shutil.copyfile(BASE, styles / 'amadeus-v1.css')
    link = '<link rel="stylesheet" href="styles/amadeus-v1.css">'
    body_class = 'am-app'
    if case == 'missing':
        link = '<link rel="stylesheet" href="styles/missing.css">'
    elif case == 'unused':
        body_class = ''
    elif case == 'custom':
        (styles / 'custom.css').write_text('body { background: white; color: black; }', encoding='utf-8')
        link = '<link rel="stylesheet" href="styles/custom.css">'
        body_class = ''
    elif case == 'import_missing':
        (styles / 'custom.css').write_text('@import "missing.css";', encoding='utf-8')
        link = '<link rel="stylesheet" href="styles/custom.css">'
    elif case == 'outside':
        (tmp_path / 'foreign.css').write_text('body { color: red; }', encoding='utf-8')
        link = '<link rel="stylesheet" href="../foreign.css">'
    elif case == 'disabled':
        link = '<link rel="stylesheet" href="styles/amadeus-v1.css" disabled>'
        body_class = ''
    elif case == 'wide':
        link += '<style>body { min-width: 1600px; }</style>'
    entry.write_text(entry.read_text(encoding='utf-8').replace('</head>', link+'</head>')
        .replace('<body>', f'<body class="{body_class}">'), encoding='utf-8')
    result = asyncio.run(validate_entry(manifest, entry, settle_milliseconds=50))
    expected_ok = case in {'applied', 'custom', 'disabled', 'wide'}
    assert result['ok'] is expected_ok, result
    codes = {d.get('code') for d in result['diagnostics']}
    if case == 'unused':
        assert 'amadeus_style_not_applied' in codes
    if case == 'outside':
        assert 'stylesheet_outside_bundle' in codes
    if case == 'wide':
        assert any(a['code']=='document_horizontal_overflow' for a in result['styleChecks']['advisories'])
    if case == 'custom':
        assert result['styleChecks']['amadeusBaseReferenced'] is False
