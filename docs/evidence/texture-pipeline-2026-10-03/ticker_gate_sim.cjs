// Compare Pixi 7.4.3 maxFPS throttling with a vsync-phase accumulator gate.
// Gate rule: budget += dt; present when budget >= capPeriod - vsyncPeriod/2; budget -= capPeriod;
// budget is clamped to [-capPeriod/2, capPeriod]; vsyncPeriod = running median of rAF deltas.
function makeGate(maxFps) {
  const deltas = [];
  let last = null, budget = 0;
  const capPeriod = maxFps > 0 ? 1000 / maxFps : 0;
  return (t) => {
    if (last === null) { last = t; return true; }
    const dt = t - last; last = t;
    deltas.push(dt); if (deltas.length > 31) deltas.shift();
    const sorted = [...deltas].sort((a, b) => a - b);
    const vsync = sorted[sorted.length >> 1];
    if (!capPeriod) return true;
    budget = Math.min(capPeriod, budget + dt);
    if (budget < capPeriod - vsync / 2) return false;
    budget = Math.max(-capPeriod / 2, budget - capPeriod);
    return true;
  };
}
function run({ refreshHz, maxFps, jitterMs, seconds, mode, stallAt }) {
  const period = 1000 / refreshHz, minElapsed = 1000 / maxFps;
  let lastFrame = -1, lastEmit = null;
  const gate = makeGate(maxFps);
  const gaps = [];
  let rng = 987654321;
  const rand = () => ((rng = (rng * 1103515245 + 12345) % 2147483648) / 2147483648);
  for (let i = 1; i < seconds * refreshHz; i++) {
    let t = 1000 + i * period + (rand() * 2 - 1) * jitterMs;
    if (stallAt && i >= stallAt) t += 250; // one 250 ms main-thread stall
    let emit;
    if (mode === "pixi") {
      const n = (t - lastFrame) | 0;
      emit = n >= minElapsed;
      if (emit) lastFrame = t - (n % minElapsed);
    } else emit = gate(t);
    if (emit) { if (lastEmit !== null) gaps.push(t - lastEmit); lastEmit = t; }
  }
  const s = [...gaps].sort((a, b) => a - b);
  const pct = (p) => s[Math.min(s.length - 1, Math.floor(p * s.length))].toFixed(1);
  const capGap = Math.max(minElapsed, period);
  return {
    mode, refreshHz, maxFps, jitterMs,
    fps: (gaps.length / seconds).toFixed(1),
    p50: pct(0.5), p95: pct(0.95), p99: pct(0.99), max: s[s.length - 1].toFixed(1),
    gapsOver1_4xCap: s.filter((g) => g > 1.4 * capGap).length,
  };
}
const rows = [];
for (const jitterMs of [0, 0.5, 1.5]) {
  for (const [refreshHz, maxFps] of [[60, 30], [60, 60], [120, 60], [144, 60], [165, 60], [240, 60], [75, 60], [144, 30], [60, 24]]) {
    for (const mode of ["pixi", "gate"]) rows.push(run({ refreshHz, maxFps, jitterMs, seconds: 60, mode }));
  }
}
rows.push(run({ refreshHz: 60, maxFps: 30, jitterMs: 0.5, seconds: 20, mode: "gate", stallAt: 600 }));
for (const r of rows) console.log(JSON.stringify(r));
