"""Memory evidence must cover the target processes and stay inside the measured interval."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'video_memory_summary', Path(__file__).parents[1]/'tools/probes/frame-video/summarize_memory.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
MIB = 2**20


def counter_row(at=1000, size=1):
    return dict(atEpochMs=at, sampleMs=1000, target=dict(allPids=[10, 20], gpuPids=[20]),
                processRam=[dict(pid=pid, privateWorkingSetBytes=size*MIB, privateCommitBytes=2*size*MIB)
                            for pid in [10, 20]],
                processGpu=[dict(pid=pid, instance=f'pid_{pid}_luid_a',
                                 dedicatedBytes=3*size*MIB, sharedBytes=size*MIB) for pid in [10, 20]],
                adapters=[dict(instance='luid_a', dedicatedBytes=100*MIB, sharedBytes=10*MIB),
                          dict(instance='luid_b', dedicatedBytes=999*MIB, sharedBytes=999*MIB)])


def test_gpu_process_is_not_sum_of_all_process_or_adapter_allocations():
    values = module.memory_values(counter_row())
    assert values['privateWorkingSetMiB'] == 2
    assert values['privateCommitMiB'] == 4
    assert values['gpuDedicatedMiB'] == 3
    assert values['gpuSharedMiB'] == 1
    assert values['adapterDedicatedMiB'] == 100


@pytest.mark.parametrize('damage', ['missing-pid', 'duplicate-pid', 'missing-ram-field', 'missing-gpu-field', 'missing-gpu'])
def test_partial_or_duplicate_process_coverage_is_not_valid_memory(damage):
    row = counter_row()
    if damage == 'missing-pid':
        row['processRam'].pop()
    elif damage == 'duplicate-pid':
        row['processRam'].append(copy.deepcopy(row['processRam'][0]))
    elif damage == 'missing-ram-field':
        del row['processRam'][0]['privateCommitBytes']
    elif damage == 'missing-gpu-field':
        del row['processGpu'][1]['sharedBytes']
    else:
        row['processGpu'].pop()
    assert module.memory_values(row) is None


def test_missing_adapter_is_unknown_instead_of_zero():
    row = counter_row()
    row['adapters'] = []
    values = module.memory_values(row)
    assert values['gpuDedicatedMiB'] == 3
    assert values['adapterDedicatedMiB'] is None


def write_run(root):
    run = root/'run'
    run.mkdir()
    native = root/'run-memory'
    native.mkdir()
    (run/'metadata.json').write_text(json.dumps(dict(options=dict(durationSeconds=30), sourceSha256={},
                                                    journey=[dict(phase='clip'), dict(phase='short-transition')])))
    (run/'summary.json').write_text(json.dumps(dict(complete=True, sourceChangedDuringRun=[], phases=[])))
    (native/'launcher.json').write_text(json.dumps(dict(samplerExitCode=0, exitCode=0, timedOut=False)))
    rows = [dict(stage='journey', phase='clip', elapsedMs=elapsed,
                 timeUtc=datetime.fromtimestamp((1000+elapsed)/1000, timezone.utc).isoformat(),
                 render=dict(textureStats={})) for elapsed in [0, 30000]]
    (run/'samples.ndjson').write_text('\n'.join(json.dumps(row) for row in rows))
    events = [dict(kind='phase-start', phase='startup', t=0), dict(kind='miss', phase='startup', t=10),
              dict(kind='phase-start', phase='clip', t=1000), dict(kind='miss', phase='clip', t=20000),
              dict(kind='miss', phase='clip', t=22000), dict(kind='miss', phase='short-transition', t=23000)]
    (run/'events.ndjson').write_text('\n'.join(json.dumps(row) for row in events))
    # Neither an early sample nor a collection straddling teardown may bias the mean.
    counters = [counter_row(at=0, size=100), counter_row(at=1000, size=1),
                counter_row(at=22000, size=2), counter_row(at=30500, size=100)]
    (native/'gpu-memory.ndjson').write_text('\n'.join(json.dumps(row) for row in counters))
    return run, native


def test_counter_collection_boundaries_and_warmup_are_respected(tmp_path):
    run, _ = write_run(tmp_path)
    result = module.summarize(run)
    assert result['validNativeSamples'] == 2
    assert result['all']['privateWorkingSetMiB']['mean'] == 3
    assert result['after20Seconds']['privateWorkingSetMiB']['mean'] == 4
    assert result['misses'] == 4
    assert result['journeyMisses'] == 3
    assert result['missesAfter20Seconds'] == 2


@pytest.mark.parametrize('failure', [dict(samplerExitCode=1), dict(exitCode=1), dict(timedOut=True)])
def test_failed_launcher_or_sampler_invalidates_measurement(tmp_path, failure):
    run, native = write_run(tmp_path)
    path = native/'launcher.json'
    outcome = json.loads(path.read_text())
    outcome.update(failure)
    path.write_text(json.dumps(outcome))
    with pytest.raises(ValueError, match='Incomplete'):
        module.summarize(run)
