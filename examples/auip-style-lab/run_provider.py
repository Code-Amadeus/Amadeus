"""One native Provider authoring run with frozen style inputs and an isolated ledger.

Reuses the shipping runtime/coordinator and existing AUIP probe setup. It does
not launch/Attach or touch the production ledger. Outputs are retained.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_host.adapters.codex_app_server import CodexAppServerAdapter
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerStore
from server.work_ledger_coordinator import WorkLedgerCoordinator
from tools.e2e_direct_codex_conversation import _create_sandbox_accessible_root
from tools.probes.probe_codex_sdk_auip_authoring import _git

HERE = Path(__file__).resolve().parent

async def run(timeout: float, seed: Path | None = None, feedback: Path | None = None):
    parent = ROOT / 'runtime' / 'auip-style-lab'
    parent.mkdir(parents=True, exist_ok=True)
    run_root = _create_sandbox_accessible_root(parent)
    workspace = run_root / 'project'
    workspace.mkdir()
    hashes = {}
    for name in ('DESIGN.md', 'base.css', 'provider-task.md'):
        shutil.copy2(HERE / name, workspace / name)
        hashes[name] = hashlib.sha256((workspace / name).read_bytes()).hexdigest()
    if seed is not None:
        for name in ('index.html', 'app.css', 'app.js', 'auip.manifest.json', 'AUTHOR_REPORT.md'):
            shutil.copy2(seed / name, workspace / name)
    if feedback is not None:
        shutil.copy2(feedback, workspace / 'REVIEW.md')
        hashes['REVIEW.md'] = hashlib.sha256((workspace / 'REVIEW.md').read_bytes()).hexdigest()
    _git(workspace, 'init', '--quiet')
    _git(workspace, 'config', 'user.email', 'amadeus-style@example.invalid')
    _git(workspace, 'config', 'user.name', 'Amadeus Style Experiment')
    _git(workspace, 'add', '.')
    _git(workspace, 'commit', '--quiet', '-m', 'Frozen visual experiment inputs')
    (parent / 'latest-run.json').write_text(json.dumps({'run_root':str(run_root), 'workspace':str(workspace)}, indent=2), encoding='utf-8')
    adapter = CodexAppServerAdapter(approval_mode='auto_review', turn_timeout_s=timeout)
    runtime = ProviderRuntime()
    store = WorkLedgerStore(run_root / 'work_ledger.sqlite3')
    coordinator = WorkLedgerCoordinator(store)
    coordinator.configure()
    runtime.set_request_preparer(coordinator.prepare_request)
    runtime.register(adapter)
    report = {'workspace': str(workspace), 'input_sha256': hashes, 'status':'running', 'reference_source_provided':False,
              'adapter_model':adapter.model, 'adapter_reasoning':adapter.reasoning_effort_label,
              'seed':str(seed) if seed else None}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    try:
        task = (workspace / 'provider-task.md').read_text(encoding='utf-8')
        if feedback is not None:
            task = '修订当前目录中已有的 Provider 首版。先读 REVIEW.md，仅修复其中已观察到的问题，保留视觉方案。不要重新设计页面。\n\n' + task
        record = await runtime.start(ProviderRunRequest(
            provider='codex', task=task, cwd=str(workspace), mode='agent',
            requirements=ProviderRequirements(task_kind='workspace_mutation', workspace_access='write',
                workspace_ownership='caller', preferred_provider='codex', preference_policy='require'),
            metadata={'source':'auip_prepare', 'source_user_text':task,
                'host_outcome_requirement': {'operation':'prepare','facet':'auip.application','expected':{'current_attempt_contribution':True}},
                'provider_manifest':CODEX_APP_SERVER_MANIFEST.to_dict()}))
        if record.task_handle is None:
            raise RuntimeError('No Provider task handle')
        cursor = 0
        with (run_root / 'events.jsonl').open('w', encoding='utf-8') as log:
            while True:
                events = list(record.events)
                for event in events[cursor:]:
                    log.write(json.dumps(event, ensure_ascii=False, default=str)+'\n')
                    if event.get('type') != 'assistant.delta':
                        print(json.dumps({'event':event.get('type'), 'time':event.get('observed_at')}, ensure_ascii=False), flush=True)
                log.flush()
                cursor = len(events)
                if record.task_handle.done():
                    break
                await asyncio.wait({record.task_handle}, timeout=5)
        await record.task_handle
        work = record.metadata.get('work') or {}
        attempt = store.get_attempt(str(work.get('attempt_id') or ''))
        report.update(status=record.status, provider_result=record.result, provider_error=record.error or '',
            work=work, outcome_verdict=attempt.metadata.get('outcome_verdict') if attempt else None,
            input_unchanged={name:hashlib.sha256((workspace/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items()},
            event_types=sorted({str(e.get('type')) for e in record.events}))
    except Exception as exc:
        report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
    finally:
        await runtime.close()
        coordinator.close()
        (run_root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if report['status']=='done' else 1

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--seed', type=Path)
    parser.add_argument('--feedback', type=Path)
    args=parser.parse_args()
    if not args.live:
        print('Dry run. Pass --live to execute one native Provider run with current configured model.')
    else:
        if bool(args.seed) != bool(args.feedback):
            parser.error('--seed and --feedback must be supplied together')
        raise SystemExit(asyncio.run(run(args.timeout, args.seed.resolve() if args.seed else None, args.feedback.resolve() if args.feedback else None)))
