"""The report uses measured intervals and distinguishes process RAM from VRAM."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'video_product_summary', Path(__file__).parents[1]/'tools/probes/frame-video/summarize_product.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_run(directory, *, complete=True, cpu_end=12):
    (directory/'metadata.json').write_text(json.dumps(dict(revision='test', options={})))
    (directory/'summary.json').write_text(json.dumps(dict(
        complete=complete, sourceChangedDuringRun=[], phases=[dict(name='idle', misses=5)])))
    rows = []
    for elapsed, seconds in [(1000, 10), (3000, cpu_end)]:
        rows.append(dict(stage='journey', phase='route-0-idle', elapsedMs=elapsed,
                         rendererProcesses=[dict(cpuSeconds=seconds, privateBytes=2**20)],
                         gpuProcesses=[dict(cpuSeconds=2, privateBytes=2*2**20)],
                         host=dict(cpuSeconds=3),
                         render=dict(textureStats=dict(residentBytes=3*2**20))))
    (directory/'samples.ndjson').write_text('\n'.join(json.dumps(row) for row in rows))


def test_measured_interval_ignores_unobserved_startup_cpu(tmp_path):
    write_run(tmp_path)
    result = module.summarize(tmp_path)['resources']['all']
    assert result['observedSeconds'] == 2
    assert result['cpuSeconds']['renderer'] == 2
    assert result['cpuOneCorePercent']['renderer'] == 100
    assert result['totalCpuOneCorePercent'] == 100
    assert result['gpuProcessPrivateMiB']['mean'] == 2
    assert result['frameStoreResidentMiB']['mean'] == 3


def test_static_slot_instrumentation_is_not_reported_as_actual_video_misses(tmp_path):
    write_run(tmp_path)
    result = module.summarize(tmp_path)
    assert result['phases'][0]['misses'] is None
    assert result['phases'][0]['legacyStaticSlotMisses'] == 5
    (tmp_path/'metadata.json').write_text(json.dumps(dict(
        revision='test', options={}, instrumentation=dict(actualMisses=True))))
    assert module.summarize(tmp_path)['phases'][0]['misses'] == 5


@pytest.mark.parametrize('complete,cpu_end,message', [
    (False, 12, 'Incomplete'), (True, 8, 'counter reset'),
])
def test_invalid_measurements_are_not_summarized(tmp_path, complete, cpu_end, message):
    write_run(tmp_path, complete=complete, cpu_end=cpu_end)
    with pytest.raises(ValueError, match=message):
        module.summarize(tmp_path)
