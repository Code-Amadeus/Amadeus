"""Read-only inventory of real runtime first hops and static prefix storage.

No renderer service, decoder, graph execution, or random draw is performed.
The JSON is a capacity estimate, never a playback-readiness guarantee.
"""
import argparse
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

ROUTES_JS = r"""
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.renderer, 'utf8');
const start = source.indexOf('class SpriteForgeRuntime {');
const end = source.indexOf('// Character rendering adapters live', start);
if (start < 0 || end < start) throw Error('Runtime class boundary changed');
let randomDraws = 0;
const math = Object.create(Math);
math.random = () => { randomDraws++; throw Error('Inventory consumed RNG'); };
const prefetched = [];
const sprite = {
  clearHold() {}, setEmotion() {},
  prefetchLabels(labels, priority, options) { prefetched.push({ labels, priority, ...options }); },
};
const context = vm.createContext({ Math: math, console: { log() {} } });
vm.runInContext(source.slice(source.indexOf('class SpriteRenderer {'), end)
  + '\nglobalThis.Runtime=SpriteForgeRuntime;globalThis.Sprite=SpriteRenderer', context);
const runtime = new context.Runtime(sprite);
runtime.loadGraph(input.payload);
const cfg = runtime.cfg;
const inputs = new Set(input.payload.graph.nodes.map(node => node.label));
for (const mapping of [cfg.triggerAliases, cfg.semanticTriggerDefaults,
  cfg.emotionEntryByIntent, cfg.postSpeechEmotionLabelByIntent, cfg.emotionIntentByLabel,
  runtime.intentAliases]) {
  for (const [key, value] of Object.entries(mapping || {})) { inputs.add(key); inputs.add(value); }
}
inputs.add(cfg.defaultSpeakingTriggerLabel); inputs.add(cfg.closedEyeSpeakingTriggerLabel);
inputs.delete(undefined);
const states = {}, unresolved = [];
for (const node of input.payload.graph.nodes) {
  runtime.currentNodeId = node.id;
  const firstHops = new Set([node.label]);
  for (const edge of runtime.graph.edges) {
    if (edge.from === node.id && Number(edge.prob || 0) > 0) firstHops.add(runtime._label(edge.to));
  }
  for (const speaking of [false, true]) {
    for (const raw of inputs) {
      let label = runtime._normalizeTriggerLabel(raw);
      const intent = runtime._emotionIntent(label);
      if (speaking && intent) label = (cfg.emotionEntryByIntent || {})[intent] || label;
      const id = runtime._findTriggerEntry(label) || runtime._nodeByLabel(label)?.id;
      if (id) firstHops.add(runtime._label(id));
      else unresolved.push({ current: node.label, speaking, raw });
    }
  }
  if (runtime._has('speakingReleaseLabels', node.label))
  for (const intent of [null, ...Object.keys(cfg.postSpeechEmotionLabelByIntent || {})]) {
    runtime.activeSpeechIntent = intent;
    firstHops.add(runtime._label(runtime._postSpeechReleaseNode(node.label)));
  }
  firstHops.delete('');
  states[node.label] = { labels: [...firstHops].sort() };
}
for (const state of Object.values(states)) {
  const twoSteps = new Set(state.labels);
  for (const label of state.labels) for (const next of states[label].labels) twoSteps.add(next);
  state.twoStepLabels = [...twoSteps].sort();
  const closure = new Set(state.labels);
  let oldSize;
  do {
    oldSize = closure.size;
    for (const label of [...closure]) for (const next of states[label].labels) closure.add(next);
  } while (oldSize !== closure.size);
  state.closureLabels = [...closure].sort();
}
const selections = {};
if (input.sampleFps) {
  context.window = { RenderBudget: require(input.renderBudget) };
  context.frameRateController = { effectiveMaxFps: input.sampleFps };
  for (const [label, clip] of Object.entries(input.timings)) {
    const sampler = Object.assign(Object.create(context.Sprite.prototype), {
      _textureSamplingEnabled: true, _textureSampleFps: input.sampleFps,
      _frameUrls: { [label]: Array(clip.count) }, _frameIntervals: { [label]: clip.intervalMs },
      _frameSamplingPlans: new Map(), _requiredFrameIndices: new Map(), _mouthConfigs: {}, _clipConfigs: {},
      _held: false, _speaking: false,
    });
    selections[label] = Array.from({ length: clip.count }, (_, index) =>
      sampler._sampleFrameIndex(label, index, index * clip.intervalMs));
  }
}
process.stdout.write(JSON.stringify({ states, inputs: [...inputs].sort(), prefetched, unresolved, randomDraws, selections }));
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sample-fps', type=int, choices=[30, 60])
    args = parser.parse_args()
    from render.character_pack import load_character_pack
    from render.spriteforge_animator import SpriteForgeAnimator
    pack = load_character_pack(args.pack)
    animator = SpriteForgeAnimator(None)
    animator._character_pack = pack
    animator._load_graph()
    payload = animator._frontend_runtime_payload()
    renderer = ROOT / 'render/web/renderer.js'
    result = subprocess.run(['node', '-e', ROUTES_JS], input=json.dumps({
        'payload': payload, 'renderer': str(renderer), 'sampleFps': args.sample_fps,
        'renderBudget': str(ROOT / 'render/web/render_budget.js'),
        'timings': {label: {'count': len(clip['frames']), 'intervalMs': clip['frameIntervalMs']}
                    for label, clip in pack.manifest['clips'].items()}}), text=True, encoding="utf-8", capture_output=True, check=True)
    inventory = json.loads(result.stdout)
    clips = pack.manifest['clips']
    selections = inventory.pop('selections')
    def selected(label, index):
        return selections[label][index] if args.sample_fps else index
    windows = [50, 80, 100, 150]
    formats = set()

    @lru_cache(maxsize=None)
    def cost(relative):
        with (args.pack / relative).open('rb') as stream:
            header = stream.read(48)
        if header[:12] != b'\xabKTX 20\xbb\r\n\x1a\n':
            raise ValueError('Not KTX2: ' + relative)
        fmt, _, width, height, depth, layers, faces, levels, _ = struct.unpack('<9I', header[12:48])
        if depth != 0 or layers != 0 or faces != 1 or levels != 1:
            raise ValueError('Estimate requires single-level 2D textures: ' + relative)
        formats.add(fmt)
        return 2 * math.ceil(width / 4) * math.ceil(height / 4) * 16

    def prefixes(labels, ms):
        return {clips[label]['frames'][selected(label, index)] for label in labels
                for index in range(min(len(clips[label]['frames']), math.ceil(ms / clips[label]['frameIntervalMs']) + 1))}

    def total(frames):
        return {'frames': len(frames), 'estimatedCpuPlusGpuBytes': sum(cost(frame) for frame in frames)}

    # Mirrors the existing required poster / closed-mouth / overlay ownership.
    # The video tail, display pin and explicit hold are additional dynamic needs.
    fixed = {clip['frames'][0] for clip in clips.values()}
    for label, profile in pack.mouth_config.get('profiles', {}).items():
        if label not in clips:
            continue
        frames = clips[label]['frames']
        closed = profile.get('closed_frame_idx')
        if isinstance(closed, int) and 0 <= closed < len(frames):
            fixed.add(frames[closed])
        openness = profile.get('openness', [])[:len(frames)]
        valid = [(value, index) for index, value in enumerate(openness)
                 if isinstance(value, (int, float)) and math.isfinite(value)]
        if valid:
            fixed.add(frames[min(valid)[1]])
    for paths in pack.mouth_overlay_paths.values():
        fixed.update(path.relative_to(args.pack).as_posix() for path in paths)

    def worst_static_window(label, reserve):
        clip = clips[label]
        frames, interval = clip['frames'], clip['frameIntervalMs']
        count = min(len(frames), math.ceil(500 / interval) + 1)
        once = clip['loopMode'] == 'once_then_hold'
        reserve_bytes = sum(cost(frame) for frame in reserve)
        largest = {'estimatedCpuPlusGpuBytes': 0}
        for source in range(len(frames)):
            for speaking in [False, True]:
                chosen, index = set(), source
                for _ in range(count):
                    chosen.add(frames[selected(label, index)])
                    if once and index == len(frames) - 1:
                        break
                    if speaking and label not in pack.mouth_config.get('profiles', {}) and not once and len(frames) > 1:
                        index = index % (len(frames) - 1) + 1
                    else:
                        index = (index + 1) % len(frames)
                amount = reserve_bytes + sum(cost(frame) for frame in chosen - reserve)
                if amount > largest['estimatedCpuPlusGpuBytes']:
                    largest = {'estimatedCpuPlusGpuBytes': amount, 'sourceIndex': source,
                               'speaking': speaking, 'windowFrames': len(chosen)}
        return largest

    global_labels = sorted(inventory['states'])
    global_reserves = {ms: prefixes(global_labels, ms) | fixed for ms in windows}
    for label, state in inventory['states'].items():
        state['prefixes'] = {}
        for ms in windows:
            frames = prefixes(state['labels'], ms)
            state['prefixes'][str(ms)] = {**total(frames), 'withExistingFixedPins': total(frames | fixed),
                'withFixedPinsAndCurrent500ms': worst_static_window(label, frames | fixed),
                'globalReserveWithCurrent500ms': worst_static_window(label, global_reserves[ms])}
    global_labels = sorted(inventory['states'])
    report = {
        'schema': 'amadeus.entry-frontier-capacity.v1',
        'sampleFps': args.sample_fps,
        'assumptions': [f'Sampling {args.sample_fps or "OFF"}; each unique URL charged once.',
            'BC7 retained CPU plus potential GPU: 2 * ceil(width/4) * ceil(height/4) * 16.',
            'Header vkFormat 0 is UASTC input, not proof of the selected GPU format.',
            'No fetch, workers, video surfaces, driver allocations, or process baseline are counted.',
            'State frontier covers known inputs, positive automatic edges, repeat, and speech release.',
            'Two-step and closure sets are conservative unions, not probability or timing predictions.',
            'Prefix readiness and sustained production rate must be tested independently.',
            'Fixed pins omit dynamic display/hold pins and the experimental video tail.',
            '500ms-window figures assume STATIC body textures; a video source changes that cost.',
            'Window maxima enumerate actual source indices and speaking modes without executing playback.'],
        'graph': {'nodes': len(payload['graph']['nodes']), 'edges': len(payload['graph']['edges'])},
        'existingFixedPins': total(fixed),
        'globalPrefixes': {str(ms): {**total(prefixes(global_labels, ms)),
            'withExistingFixedPins': total(prefixes(global_labels, ms) | fixed)} for ms in windows},
        **inventory,
    }
    report['inputKtxVkFormats'] = sorted(formats)
    source_paths = {name: ROOT / name for name in [
        'tools/probes/frame-video/inspect_entry_frontier.py', 'render/web/renderer.js',
        'render/spriteforge_animator.py', 'render/character_pack.py', 'render/web/render_budget.js']}
    source_paths.update({name: args.pack / name for name in [
        'runtime_manifest.json', pack.manifest['graph'], pack.manifest['mouthConfig']]})
    report['sourceSha256'] = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                             for name, path in source_paths.items()}
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
    print(json.dumps({'output': str(args.output), 'graph': report['graph'],
        'knownInputs': len(inventory['inputs']), 'unresolved': len(inventory['unresolved']),
        'randomDraws': inventory['randomDraws'], 'fixed': report['existingFixedPins'],
        'frontierRange': [min(len(s['labels']) for s in inventory['states'].values()),
                          max(len(s['labels']) for s in inventory['states'].values())],
        'twoStepRange': [min(len(s['twoStepLabels']) for s in inventory['states'].values()),
                        max(len(s['twoStepLabels']) for s in inventory['states'].values())],
        'closureRange': [min(len(s['closureLabels']) for s in inventory['states'].values()),
                        max(len(s['closureLabels']) for s in inventory['states'].values())],
        'global': report['globalPrefixes']}, indent=2))


if __name__ == '__main__':
    main()
