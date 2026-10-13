"""Summarize measured product probe samples without treating GPU private RAM as VRAM."""
import argparse
import json
import math
from pathlib import Path
import statistics


def cpu(row):
    groups = {name: sum(p['cpuSeconds'] for p in row.get(field, []))
              for name, field in [('renderer', 'rendererProcesses'), ('gpu', 'gpuProcesses'),
                                  ('other', 'otherProcesses')]}
    groups['host'] = row.get('host', {}).get('cpuSeconds', 0)
    return groups


def quantiles(values):
    values = sorted(values)
    return dict(mean=statistics.mean(values), p95=values[max(0, math.ceil(len(values)*.95)-1)],
                max=max(values)) if values else None


def summarize(directory):
    metadata = json.loads((directory/'metadata.json').read_text())
    outcome = json.loads((directory/'summary.json').read_text())
    if not outcome['complete'] or outcome['sourceChangedDuringRun']:
        raise ValueError(f'Incomplete or changed-source run: {directory.name}')
    rows = [json.loads(line) for line in (directory/'samples.ndjson').read_text().splitlines()]
    rows = [row for row in rows if row['stage'] in ('journey', 'journey-complete')]
    groups = {'all': rows}
    for row in rows:
        groups.setdefault(row['phase'], []).append(row)
    summaries = {}
    for name, samples in groups.items():
        # Use only adjacent samples wholly within this phase. Transition gaps
        # are deliberately excluded from phase CPU (but included in all).
        seconds = (samples[-1]['elapsedMs']-samples[0]['elapsedMs'])/1000
        if seconds <= 0:
            continue
        first, last = cpu(samples[0]), cpu(samples[-1])
        cpu_seconds = {key: last[key]-first[key] for key in first}
        if any(value < 0 for value in cpu_seconds.values()):
            raise ValueError('Process counter reset; CPU comparison invalid')
        memories = {name: quantiles([sum(p['privateBytes'] for p in row.get(field, []))/2**20
                                    for row in samples])
                    for name, field in [('rendererPrivateMiB', 'rendererProcesses'),
                                        ('gpuProcessPrivateMiB', 'gpuProcesses')]}
        summaries[name] = dict(samples=len(samples), observedSeconds=seconds,
                               cpuSeconds=cpu_seconds,
                               cpuOneCorePercent={key: value/seconds*100 for key, value in cpu_seconds.items()},
                               totalCpuOneCorePercent=sum(cpu_seconds.values())/seconds*100,
                               **memories,
                               frameStoreResidentMiB=quantiles([row['render']['textureStats']['residentBytes']/2**20
                                                               for row in samples]))
    final = rows[-1]['render']
    actual_misses = metadata.get('instrumentation', {}).get('actualMisses', False)
    phases = outcome['phases']
    if not actual_misses:
        phases = [dict(row, misses=None, legacyStaticSlotMisses=row.get('misses')) for row in phases]
    return dict(run=directory.name, revision=metadata['revision'], options=metadata['options'],
                phases=phases, displayMissSource='renderer-counter' if actual_misses else 'legacy-static-slot-only',
                resources=summaries, finalTextureStats=final['textureStats'],
                video=final.get('video'),
                limitation='Single local run per condition; sampled phase CPU excludes transition intervals. '
                'CPU percentages use one logical core as 100%. GPU process private memory is not VRAM. '
                'FrameStore bytes exclude video decoder surfaces; output textures are separately counted in video stats.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path, help='Optional new derived report path')
    args = parser.parse_args()
    result = summarize(args.run)
    with (args.output or args.run/'product-summary.json').open('x', encoding='utf-8') as output:
        json.dump(result, output, indent=2)
    print(json.dumps(dict(run=result['run'], resources=result['resources']['all'],
                         finalTextureStats=result['finalTextureStats'])))


if __name__ == '__main__':
    main()
