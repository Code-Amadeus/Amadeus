(function () {
  "use strict";

  const TICK_MS = 200;
  const CHECKPOINT_TICKS = 5;
  const WARNING_HEAT = 70;
  const STABLE_HEAT = 44;
  const manifest = JSON.parse(document.getElementById("auip-manifest").textContent);
  const heatElement = document.getElementById("heat");
  const coolingElement = document.getElementById("cooling");
  const trendElement = document.getElementById("trend");
  const ticksElement = document.getElementById("ticks");
  const revisionElement = document.getElementById("revision");
  const statusElement = document.getElementById("status");
  const connectionElement = document.getElementById("connection");
  const dialElement = document.getElementById("dial");
  let heat = 56;
  let cooling = 0;
  let tickCount = 0;
  let stableTicks = 0;
  let status = "running";
  let warningPublished = false;
  let terminalPublished = false;
  let awaitingControl = false;
  let attached = false;
  let auip = null;
  let semanticEventCount = 0;

  function trend() {
    const delta = 2 - cooling * 1.5;
    return delta > 0 ? "rising" : delta < 0 ? "falling" : "steady";
  }

  function snapshot() {
    return {
      metrics: window.AmadeusAUIPSituations.scalarSituation({
        metrics: [{
          id: "heat",
          label: "Core heat",
          value: Number(heat.toFixed(1)),
          unit: "percent",
          trend: trend(),
          safe: [STABLE_HEAT, WARNING_HEAT],
        }],
      }),
      actions: window.AmadeusAUIPSituations.choiceSituation({
        actionTypes: ["reactor.set_cooling"],
        options: [0, 1, 2, 3].map(level => ({
          id: `cooling-${level}`,
          label: `Cooling level ${level}`,
          action: "reactor.set_cooling",
          payload: {level},
          available: status === "running" && (
            !attached || (awaitingControl && level >= 2)
          ),
        })),
      }),
      cooling,
      trend: trend(),
      elapsedSeconds: Number((tickCount * TICK_MS / 1000).toFixed(1)),
      status,
      warningActive: heat >= WARNING_HEAT,
    };
  }

  function render() {
    const bounded = Math.max(0, Math.min(100, heat));
    dialElement.style.setProperty("--heat-angle", `${bounded * 3.6}deg`);
    heatElement.textContent = heat.toFixed(1);
    coolingElement.textContent = String(cooling);
    trendElement.textContent = trend();
    ticksElement.textContent = String(tickCount);
    revisionElement.textContent = String(auip ? auip.revision() : 0);
    statusElement.textContent = status === "stabilized" ? "Stable" : heat >= WARNING_HEAT ? "Heat warning" : "Drifting";
    connectionElement.textContent = attached ? "Attached to Amadeus" : "Standalone mode";
    document.querySelectorAll("button[data-level]").forEach(button => {
      button.classList.toggle("active", Number(button.dataset.level) === cooling);
      button.disabled = status !== "running" || (
        attached && (!awaitingControl || Number(button.dataset.level) < 2)
      );
    });
  }

  function publishCheckpoint(event) {
    if (!attached || !auip) return null;
    if (event) semanticEventCount += 1;
    const committed = auip.checkpoint({
      actor: event ? event.actor : "system",
      events: event ? [event] : [],
    });
    render();
    committed.publication.catch(error => console.error("AUIP reactor publication failed", error));
    return committed;
  }

  function commitLocalCooling(level, actor) {
    if (status !== "running") return {ok: false, reason: "simulation already stabilized"};
    if (attached && !awaitingControl) return {ok: false, reason: "no stable control window is open"};
    if (attached && level < 2) return {ok: false, reason: "that level cannot reduce the current warning"};
    if (!Number.isInteger(level) || level < 0 || level > 3) return {ok: false, reason: "cooling level out of range"};
    if (level === cooling) return {ok: false, reason: "cooling level unchanged"};
    const previous = cooling;
    cooling = level;
    awaitingControl = false;
    stableTicks = 0;
    render();
    return {ok: true, previous, level, actor};
  }

  function coolingEvent(change, actor) {
    return {
      type: "reactor.cooling_changed",
      actor,
      payload: {previousLevel: change.previous, level: change.level, heat: Number(heat.toFixed(1)), trend: trend()},
    };
  }

  function tick() {
    if (status !== "running") return;
    if (attached && awaitingControl) return;
    tickCount += 1;
    heat = Math.max(20, Math.min(100, heat + 2 - cooling * 1.5));
    if (cooling >= 2 && heat <= STABLE_HEAT) stableTicks += 1;
    else stableTicks = 0;

    if (attached && !warningPublished && heat >= WARNING_HEAT) {
      warningPublished = true;
      awaitingControl = true;
      publishCheckpoint({
        type: "reactor.heat_warning",
        actor: "system",
        payload: {heat: Number(heat.toFixed(1)), threshold: WARNING_HEAT, trend: trend()},
      });
    } else if (attached && stableTicks < 3 && tickCount % CHECKPOINT_TICKS === 0) {
      publishCheckpoint(null);
    }

    if (attached && stableTicks >= 3 && !terminalPublished) {
      terminalPublished = true;
      status = "stabilized";
      publishCheckpoint({
        type: "reactor.stabilized",
        actor: "system",
        payload: {heat: Number(heat.toFixed(1)), cooling, elapsedSeconds: Number((tickCount * TICK_MS / 1000).toFixed(1))},
      });
    }
    render();
  }

  document.querySelectorAll("button[data-level]").forEach(button => {
    button.addEventListener("click", () => {
      const level = Number(button.dataset.level);
      if (!auip) {
        commitLocalCooling(level, "user");
        return;
      }
      const committed = auip.commitLocal({
        actor: "user",
        mutate: () => commitLocalCooling(level, "user"),
        effects: ({result, state}) => ({
          cooling: {
            previousLevel: result.previous,
            level: result.level,
            trend: state.trend,
            label: `cooling level ${result.level}`,
          },
        }),
        events: ({result}) => [coolingEvent(result, "user")],
      });
      if (committed.committed) semanticEventCount += 1;
      render();
      committed.publication.catch(error => console.error("AUIP cooling publication failed", error));
    });
  });

  async function attach() {
    if (!window.AmadeusAUIP || !window.AmadeusAUIPManaged) return;
    const launch = window.AmadeusAUIP.readLaunchConfig();
    auip = window.AmadeusAUIP.createManagedApp({
      manifest,
      ...(launch
        ? {
            attachTicket: launch.attachTicket,
            transport: window.AmadeusAUIP.createWebSocketTransport({url: launch.webSocketUrl}),
          }
        : {selfAttach: false}),
      snapshot,
      initialEvents: [{
        type: "simulation.ready",
        actor: "app",
        payload: {tickMs: TICK_MS, checkpointTicks: CHECKPOINT_TICKS},
      }],
      actions: {
        "reactor.set_cooling": (payload, tx) => {
          const outcome = tx.commit({
            mutate: () => commitLocalCooling(Number(payload.level), "kurisu"),
            effects: ({result, state}) => ({
              cooling: {
                previousLevel: result.previous,
                level: result.level,
                trend: state.trend,
                label: `cooling level ${result.level}`,
              },
            }),
            events: ({result}) => [coolingEvent(result, "kurisu")],
          });
          if (outcome.committed) semanticEventCount += 1;
          render();
          return outcome;
        },
      },
    });
    if (!launch) return;
    try {
      attached = true;
      render();
      await auip.start();
      semanticEventCount += 1;
      render();
    } catch (_error) {
      attached = false;
      render();
    }
  }

  window.__auipReactor = {
    snapshot,
    isAttached: () => attached,
    revision: () => auip ? auip.revision() : 0,
    tickCount: () => tickCount,
    semanticEventCount: () => semanticEventCount,
    async settled() {
      if (auip) await auip.settled();
      return snapshot();
    },
    async close(reason) {
      if (!auip) return {ok: true};
      await auip.settled();
      const result = await auip.close(reason || "app_closed");
      attached = false;
      render();
      return result;
    },
  };

  render();
  window.setInterval(tick, TICK_MS);
  void attach();
})();
