"""Join Windows memory counters to the product's measured timeline."""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import statistics


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines()]


def distribution(values):
    values = sorted(values)
    if not values:
        return None
    return dict(mean=statistics.mean(values), min=values[0], max=values[-1],
                p95=values[math.ceil(len(values)*.95)-1])


def memory_values(row):
    target = row.get('target') or {}
    ram = row.get('processRam', [])
    expected = set(target.get('allPids', []))
    valid_ram = expected and len(ram) == len(expected) and {item['pid'] for item in ram} == expected and all(
        all(key in item for key in ('privateWorkingSetBytes', 'privateCommitBytes')) for item in ram)
    gpu = [item for item in row.get('processGpu', []) if item['pid'] in target.get('gpuPids', [])]
    valid_gpu = gpu and {item['pid'] for item in gpu} == set(target.get('gpuPids', [])) and all(
        all(key in item for key in ('dedicatedBytes', 'sharedBytes')) for item in gpu)
    if not valid_ram or not valid_gpu:
        return None
    # GPU-process counters are reported separately, never summed across app
    # processes as physical VRAM: shared allocations can otherwise be counted twice.
    adapter_ids = {'luid_' + item['instance'].split('_luid_', 1)[1] for item in gpu}
    adapters = [item for item in row.get('adapters', []) if item['instance'] in adapter_ids]
    valid_adapters = {item['instance'] for item in adapters} == adapter_ids and all(
        all(key in item for key in ('dedicatedBytes', 'sharedBytes')) for item in adapters)
    mib = 1048576
    return dict(privateWorkingSetMiB=sum(item['privateWorkingSetBytes'] for item in ram)/mib,
                privateCommitMiB=sum(item['privateCommitBytes'] for item in ram)/mib,
                gpuDedicatedMiB=sum(item['dedicatedBytes'] for item in gpu)/mib,
                gpuSharedMiB=sum(item['sharedBytes'] for item in gpu)/mib,
                adapterDedicatedMiB=sum(item['dedicatedBytes'] for item in adapters)/mib if valid_adapters else None,
                adapterSharedMiB=sum(item['sharedBytes'] for item in adapters)/mib if valid_adapters else None)


def summarize(run):
    meta, outcome = read(run/'metadata.json'), read(run/'summary.json')
    launcher = read(Path(str(run)+'-memory')/'launcher.json')
    if (not outcome['complete'] or outcome['sourceChangedDuringRun'] or launcher['samplerExitCode'] != 0
            or launcher['exitCode'] != 0 or launcher['timedOut']):
        raise ValueError('Incomplete or changed-source measurement')
    samples = lines(run/'samples.ndjson')
    journey = [row for row in samples if row['stage'] in ('journey', 'journey-complete')]
    start = datetime.fromisoformat(journey[0]['timeUtc'].replace('Z', '+00:00')).timestamp()*1000-journey[0]['elapsedMs']
    end = start+journey[-1]['elapsedMs']
    native = lines(Path(str(run)+'-memory')/'gpu-memory.ndjson')
    measured, steady = [], []
    invalid = 0
    for row in native:
        at = row.get('atEpochMs', 0)
        # Get-Counter collection takes about a second. Exclude samples crossing
        # either journey boundary, especially teardown after the final tick.
        if not (start <= at and at+row.get('sampleMs', 0) <= end):
            continue
        values = memory_values(row)
        if values is None:
            invalid += 1
            continue
        measured.append(values)
        if at >= start+20000:
            steady.append(values)
    if not measured:
        raise ValueError('No complete process/GPU counter samples in measured interval')
    def metrics(rows):
        return {key: distribution([row[key] for row in rows if row[key] is not None]) for key in measured[0]}
    events = lines(run/'events.ndjson')
    # A short transition can start and end between the two-second RAM samples.
    # Its display misses still belong to the measured route.
    journey_phases = {row['phase'] for row in meta['journey']}
    first_phase = next(event['t'] for event in events
                       if event['kind'] == 'phase-start' and event['phase'] == journey[0]['phase'])
    journey_misses = [row for row in events if row['kind'] == 'miss' and row['phase'] in journey_phases]
    released = [row for row in samples if row['stage'] == 'resources-released']
    video = journey[-1]['render'].get('video')
    return dict(run=run.name, textureBudgetMiB=meta['options'].get('textureBudgetMiB', 2048),
                videoEnabled=bool(meta['options'].get('videoFixture')), onlyClip=meta['options'].get('onlyClip'),
                seconds=meta['options']['durationSeconds'], validNativeSamples=len(measured), invalidNativeSamples=invalid,
                all=metrics(measured), after20Seconds=metrics(steady),
                nativeCounterErrors=[row.get('error') for row in native if row.get('error')],
                misses=sum(row['kind'] == 'miss' for row in events),
                journeyMisses=len(journey_misses),
                missesAfter20Seconds=sum(row['t'] >= first_phase+20000 for row in journey_misses),
                phases=outcome['phases'], finalTextureStats=journey[-1]['render']['textureStats'],
                finalVideo={key:value for key,value in (video or {}).items() if key not in ('recentCompleted','active')},
                release=([dict(textureStats=row['render']['textureStats'],
                               videoActive=len((row['render'].get('video') or {}).get('active', [])))
                          for row in released][-1] if released else None),
                counterSourceSha256=launcher.get('sourceSha256'),
                productSourceSha256=meta['sourceSha256'],
                limitations=['Steady window excludes the first 20 seconds; mixed routes still include transitions.',
                             'Private working set is private resident RAM; private commit is committed virtual memory.',
                             'GPU process dedicated/shared counters can include shared allocations and have known driver/OS caveats.',
                             'Adapter counters include other applications; use them to cross-check direction and release, not as isolated app usage.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path, help='Optional new derived report path')
    args = parser.parse_args()
    report = summarize(args.run)
    with (args.output or args.run/'memory-summary.json').open('x', encoding='utf-8') as output:
        json.dump(report, output, indent=2)
    print(json.dumps({key: report[key] for key in ['run','validNativeSamples','invalidNativeSamples',
                                                  'after20Seconds','misses','missesAfter20Seconds','finalTextureStats']}))


if __name__ == '__main__':
    main()
