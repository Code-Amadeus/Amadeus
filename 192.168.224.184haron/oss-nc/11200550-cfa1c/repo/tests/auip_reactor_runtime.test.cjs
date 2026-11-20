const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = process.cwd();
const level = Number(process.argv[2]);
let tick, options, revision = 1;
const publications = [];
const element = () => ({style: {setProperty() {}}, textContent: '', classList: {toggle() {}}});
const elements = new Map();
const manifest = fs.readFileSync(path.join(root, 'examples/auip-reactor/auip.manifest.json'), 'utf8');
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    if (id === 'auip-manifest') elements.get(id).textContent = manifest;
    return elements.get(id);
  },
  querySelectorAll() { return []; },
};
const window = {
  setInterval(callback) { tick = callback; },
  AmadeusAUIPSituations: {scalarSituation: x => x, choiceSituation: x => x},
  AmadeusAUIPManaged: {},
  AmadeusAUIP: {
    readLaunchConfig: () => ({attachTicket: 'synthetic', webSocketUrl: 'unused'}),
    createWebSocketTransport: () => ({}),
    createManagedApp(config) {
      options = config;
      return {
        revision: () => revision,
        start: async () => {},
        checkpoint(event) {
          revision++;
          publications.push({event, state: config.snapshot()});
          return {publication: Promise.resolve()};
        },
      };
    },
  },
};
vm.runInNewContext(fs.readFileSync(path.join(root, 'examples/auip-reactor/simulation.js'), 'utf8'), {window, document, console});
for (let i = 0; i < 100; i++) tick();
assert.equal(window.__auipReactor.snapshot().status, 'running');
assert.equal(window.__auipReactor.snapshot().metrics.metrics[0].value, 70);
const result = options.actions['reactor.set_cooling']({level}, {
  commit({mutate}) { const result = mutate(); return {committed: result.ok}; },
});
assert.equal(result.committed, true);
for (let i = 0; i < 100; i++) tick();
const state = window.__auipReactor.snapshot();
const metric = state.metrics.metrics[0];
assert.equal(state.status, 'stabilized');
assert.ok(metric.value >= metric.safe[0] && metric.value <= metric.safe[1], `terminal heat ${metric.value} is outside ${metric.safe}`);
assert.equal(metric.trend, 'steady');
const completed = publications.filter(x => x.event.events.some(e => e.type === 'reactor.stabilized'));
assert.equal(completed.length, 1);
for (let i = 0; i < 100; i++) tick();
assert.equal(window.__auipReactor.snapshot().metrics.metrics[0].value, metric.value);
console.log(`cooling ${level}: stable inside declared safe interval`);
