"""Qualify core Electron startup with isolated damaged/relocated cleanup state.

Run after building Electron: python -m tools.probes.backend_cleanup_startup
Uses the existing model-less application journey; no chat turn or model is run.
"""

import asyncio
import json
from pathlib import Path

from tools import smoke_electron_model_less as smoke
from tools.smoke_electron_model_less import ROOT
from tools.e2e_live_product_journey import ElectronProduct

original_start = ElectronProduct.start

async def main():
    results = []
    for mode in ['ordinary', 'zero-filled', 'missing-index', 'ancestor-junction']:
        links = []
        async def start(self, **kwargs):
            state = self.run_root / 'state'
            if mode == 'ancestor-junction':
                import _winapi
                target = self.run_root / 'relocated-state'
                target.mkdir()
                _winapi.CreateJunction(str(target), str(state))
                links.append((state, target))
            data = state / 'electron-user-data'
            data.mkdir(parents=True, exist_ok=True)
            index = data / 'backend-cleanup.json'
            if mode == 'zero-filled':
                index.write_bytes(bytes(128))
            elif mode == 'missing-index':
                Path(str(index) + '.receipts').mkdir()
            return await original_start(self, **kwargs)
        ElectronProduct.start = start
        try:
            args = smoke._parser().parse_args(['--report-dir', str(ROOT / 'build' / f'pr261-r3-app-{mode}')])
            code, report = await smoke.run_smoke(args)
            if code == 0:
                run_root = Path(report['paths']['run_root'])
                index = run_root / 'state/electron-user-data/backend-cleanup.json'
                if mode == 'zero-filled':
                    assert index.read_bytes() == bytes(128), 'damaged evidence was overwritten'
                elif mode == 'missing-index':
                    assert not index.exists() and Path(str(index) + '.receipts').is_dir()
                else:
                    evidence = json.loads(index.read_text())
                    assert evidence['owners'] == [] and evidence['unknown'] is False
                if mode in {'zero-filled', 'missing-index'}:
                    assert 'backend cleanup evidence unavailable' in Path(report['paths']['process_log']).read_text(encoding='utf-8')
                report['checks']['cleanup_evidence_preserved_or_verified'] = True
            results.append({'scenario': mode, 'exit_code': code, 'status': report['status'],
                            'checks': report['checks'], 'report': report['paths']['report'],
                            'error': report.get('error')})
            print(json.dumps(results[-1]), flush=True)
        finally:
            ElectronProduct.start = original_start
            for link, target in links:
                assert link.is_junction() and link.resolve() == target.resolve()
                assert target.resolve().is_relative_to(link.parent.resolve())
                link.rmdir()  # Remove only this test's junction, never its target.
    (ROOT / 'build' / 'pr261-r3-app-results.json').write_text(json.dumps(results, indent=2))
    return int(any(item['exit_code'] for item in results))

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
