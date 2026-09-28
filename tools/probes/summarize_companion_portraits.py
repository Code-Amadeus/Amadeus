"""Summarize measured process commit, CPU and actual frame advances; no estimates as measurements."""
import argparse
import json
from pathlib import Path
from statistics import median


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment', type=Path)
    args = parser.parse_args()
    runs = []
    for directory in sorted((args.experiment / 'runs').iterdir()):
        result_file = directory / 'result.json'
        if not result_file.exists():
            continue
        result = json.loads(result_file.read_text())
        if not result['ok']:
            runs.append({'run': directory.name, 'error': result})
            continue
        metadata = json.loads((directory / 'metadata.json').read_text())
        samples = json.loads((directory / 'samples.json').read_text())
        phases = {}
        for phase in dict.fromkeys(s['phase'] for s in samples):
            rows = [r for r in samples if r['phase'] == phase]
            # Exclude first second of each phase from steady interval metrics.
            settled = rows[1:] or rows
            page = [next(p for p in row['processes'] if p['pid'] == row['rendererPid']) for row in settled]
            def total_cpu(row):
                return sum(p['cpu'].get('cumulativeCPUUsage', 0) for p in row['processes'])
            elapsed = (rows[-1]['time'] - rows[0]['time']) / 1000
            def advances(row):
                return row['state']['atlas']['draws'] if row['state']['atlas'] else row['state']['baselineChanges']
            phases[phase] = {
                'totalPrivateMiB': round(median(sum(p['memoryKiB']['privateBytes'] for p in r['processes'])/1024 for r in settled), 2),
                'pagePrivateMiB': round(median(p['memoryKiB']['privateBytes']/1024 for p in page), 2),
                'gpuPrivateMiB': round(median(sum(p['memoryKiB']['privateBytes'] for p in r['processes'] if p['type']=='GPU')/1024 for r in settled), 2),
                'cpuReportedPercent': round(median(sum(p['cpu']['percentCPUUsage'] for p in r['processes']) for r in settled), 3),
                'cpuMillisecondsPerWallSecond': round(1000*(total_cpu(rows[-1])-total_cpu(rows[0]))/elapsed, 2) if elapsed else None,
                'portraitUpdatesPerSecond': round((advances(rows[-1])-advances(rows[0]))/elapsed, 2) if elapsed else None,
                'residentAtlasMiB': round((rows[-1]['state']['atlas'] or {}).get('residentBytes',0)/2**20, 2),
                'sampleCount':len(rows),
            }
        runs.append({'run':directory.name,'variant':metadata['variant'],'offscreen':metadata['offscreen'],
                     'startupMs':round(metadata['startupMs'],1),'phases':phases,
                     'sampledPeakTotalPrivateMiB':round(max(sum(p['memoryKiB']['privateBytes'] for p in r['processes']) for r in samples)/1024,2),
                     'maxResidentAtlasMiB':round(max((r['state']['atlas'] or {}).get('residentBytes',0) for r in samples)/2**20,2)})
    output = {'units':'MiB = 2^20 bytes; private committed process memory, not VRAM or working set. CPU percentages are Electron API values.',
              'limitations':'Short runs on one machine; 1s samples miss brief peaks. Offscreen has readback overhead. Native runs include common rAF instrumentation. No game benchmark.',
              'runs':runs}
    (args.experiment / 'summary.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    for run in runs:
        if 'error' in run:
            print(json.dumps(run)); continue
        print(run['run'])
        for phase in ['normal-speaking','thinking-speaking','churn-3','standby','paused']:
            if phase in run['phases']:
                row=run['phases'][phase]
                print(f"  {phase:20} total={row['totalPrivateMiB']:7.2f} page={row['pagePrivateMiB']:6.2f} GPU={row['gpuPrivateMiB']:7.2f} CPU={row['cpuReportedPercent']:5.3f}% updates={row['portraitUpdatesPerSecond']}")


if __name__ == '__main__':
    main()
