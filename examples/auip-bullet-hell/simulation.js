(function () {
  "use strict";

  const manifestNode = document.getElementById("auip-manifest");
  const manifest = JSON.parse(manifestNode.textContent);
  const $ = (id) => document.getElementById(id);
  const state = {
    wave: "danger",
    enemies: 9,
    bullets: 48,
    danger: 92,
    health: 32,
    dodges: 0,
    shots: 0,
    rewards: 1,
    rewardRisk: 90,
    follows: 0,
    collected: 0,
    controllerFrames: 0,
    playerX: 48,
    lastCommand: "idle",
  };
  const CONTROL_TICK_MS = 100;
  const CHECKPOINT_INTERVAL_MS = 1000;
  const EFFECT_INTERVAL_MS = Object.freeze({
    dodge: 420,
    fire: 280,
    follow: 480,
    collect: 520,
  });
  let policy = null;
  let controllerStatus = {
    kind: "controller/v1",
    status: "idle",
    policyRevision: null,
    policyAction: null,
    policySummary: "",
  };
  let connected = false;
  let auip = null;
  let policyEffectReported = false;
  let lastPublishedCommand = "";
  let lastEffectAtMs = 0;
  let lastAnimationAtMs = 0;
  let controlIntent = {kind: "idle", targetX: state.playerX};

  function pressureBand(value, lightMax, moderateMax) {
    if (value <= 0) return "none";
    if (value <= lightMax) return "light";
    if (value <= moderateMax) return "moderate";
    return "dense";
  }

  function countBand(value, fewMax, severalMax) {
    if (value <= 0) return "none";
    if (value <= fewMax) return "few";
    if (value <= severalMax) return "several";
    return "many";
  }

  function semanticField() {
    return {
      enemyPressure: countBand(state.enemies, 2, 5),
      projectilePressure: pressureBand(state.bullets, 5, 18),
      healthCondition: state.health < 35
        ? "critical"
        : (state.health < 60 ? "strained" : "stable"),
      rewardOpportunity: countBand(state.rewards, 1, 4),
    };
  }

  function snapshot(context) {
    const governance = context && context.controller
      ? context.controller
      : controllerStatus;
    return {
      tactics: AmadeusAUIPSituations.choiceSituation({
        compact: true,
        action: "battle.set_tactics",
        actionTypes: ["battle.set_tactics"],
        options: [
          {id: "evade", label: "Evade", payload: {mode: "evade"}, available: true},
          {id: "balance", label: "Balance", payload: {mode: "balance"}, available: true},
          {id: "attack", label: "Attack", payload: {mode: "attack"}, available: true},
          {id: "follow", label: "Follow", payload: {mode: "follow"}, available: true},
          {id: "rewards", label: "Rewards", payload: {mode: "rewards"}, available: true},
        ],
      }),
      field: semanticField(),
      controller: AmadeusAUIPSituations.controllerSituation(governance),
    };
  }

  function renderMotion() {
    $("player").style.left = `${state.playerX}%`;
    $("laser").style.left = `${state.playerX}%`;
  }

  function render(renderEntities = true) {
    $("enemy-count").textContent = String(state.enemies);
    $("bullet-count").textContent = String(state.bullets);
    $("health").textContent = String(state.health);
    $("dodge-count").textContent = String(state.dodges);
    $("shot-count").textContent = String(state.shots);
    $("reward-available").textContent = String(state.rewards);
    $("follow-count").textContent = String(state.follows);
    $("reward-count").textContent = String(state.collected);
    $("controller-frame-count").textContent = String(state.controllerFrames);
    $("last-command").textContent = state.lastCommand;
    renderMotion();
    $("reward-orb").classList.toggle("collected", state.rewards === 0);
    $("connection").textContent = connected ? "AUIP 已连接" : "独立模式";
    const badge = $("policy-badge");
    badge.textContent = policy && controllerStatus.status === "active"
      ? `策略：${policy.mode}`
      : "策略未接管";
    badge.className = policy && controllerStatus.status === "active" ? "active" : "";
    const statusByWave = {
      danger: "敌人正在释放密集弹幕。",
      calm: "弹幕之间出现了明显空隙。",
      follow: "玩家标记位于编队另一侧。",
      rewards: "场上出现了多枚奖励光点。",
    };
    $("status").textContent = statusByWave[state.wave] || statusByWave.calm;
    if (renderEntities) renderArena();
  }

  function renderArena() {
    const root = $("entities");
    root.textContent = "";
    const enemies = Math.min(state.enemies, 12);
    const bullets = Math.min(state.bullets, 54);
    for (let index = 0; index < enemies; index += 1) {
      const node = document.createElement("div");
      node.className = "enemy";
      node.style.left = `${7 + (index * 83) % 86}%`;
      node.style.top = `${20 + (index % 3) * 34}px`;
      node.style.animationDelay = `${-(index % 5) * 0.22}s`;
      root.appendChild(node);
    }
    for (let index = 0; index < bullets; index += 1) {
      const node = document.createElement("div");
      node.className = "bullet";
      node.style.left = `${3 + (index * 37) % 94}%`;
      node.style.top = `${78 + (index * 29) % 210}px`;
      node.style.animationDelay = `${-(index % 11) * 0.11}s`;
      root.appendChild(node);
    }
  }

  function setWave(kind) {
    if (kind === "calm") {
      Object.assign(state, {wave: "calm", enemies: 2, bullets: 4, danger: 18, health: 90, rewards: 1, rewardRisk: 15, playerX: 48, lastCommand: "idle"});
    } else if (kind === "follow") {
      Object.assign(state, {wave: "follow", enemies: 5, bullets: 14, danger: 38, health: 70, rewards: 1, rewardRisk: 30, playerX: 24, lastCommand: "idle"});
    } else if (kind === "rewards") {
      Object.assign(state, {wave: "rewards", enemies: 3, bullets: 10, danger: 30, health: 78, rewards: 6, rewardRisk: 25, playerX: 48, lastCommand: "idle"});
    } else {
      Object.assign(state, {wave: "danger", enemies: 9, bullets: 48, danger: 92, health: 32, rewards: 1, rewardRisk: 90, playerX: 48, lastCommand: "idle"});
    }
    controlIntent = {kind: "idle", targetX: state.playerX};
    render();
  }

  function localWaveChange(kind) {
    if (!auip) {
      setWave(kind);
      return;
    }
    const envelope = auip.commitLocal({
      actor: "user",
      mutate: () => setWave(kind),
      effects: {wave: kind},
      events: [{type: "battle.wave_changed", actor: "user", payload: {wave: kind}}],
    });
    envelope.publication.catch(() => {});
  }

  function controllerObserve() {
    return Object.freeze({
      enemies: state.enemies,
      bullets: state.bullets,
      danger: state.danger,
      health: state.health,
      rewards: state.rewards,
      rewardRisk: state.rewardRisk,
      playerX: state.playerX,
      wave: state.wave,
    });
  }

  function controllerDecide({policy: activePolicy, observation}) {
    if (!activePolicy) return null;
    if (observation.danger > 65 || observation.health < 35) {
      return {kind: "dodge"};
    }
    if (activePolicy.mode === "evade") return {kind: "dodge"};
    if (activePolicy.mode === "attack") return {kind: "fire"};
    if (activePolicy.mode === "follow") return {kind: "follow"};
    if (activePolicy.mode === "rewards") {
      return observation.rewards > 0 ? {kind: "collect"} : {kind: "follow"};
    }
    if (observation.danger > 45 || observation.health < 45) {
      return {kind: "dodge"};
    }
    if (observation.rewards > 0 && observation.rewardRisk <= 35) {
      return {kind: "collect"};
    }
    return {kind: "fire"};
  }

  function targetForCommand(command) {
    if (command.kind === "dodge") {
      if (
        controlIntent.kind === "dodge"
        && Math.abs(state.playerX - controlIntent.targetX) > 5
      ) {
        return controlIntent.targetX;
      }
      return state.playerX < 50 ? 76 : 24;
    }
    if (command.kind === "follow") return 80;
    if (command.kind === "collect") return 22 + (state.collected % 2) * 14;
    return 48;
  }

  function applySemanticOutcome(command, nowMs, commandChanged) {
    const interval = EFFECT_INTERVAL_MS[command.kind] || 500;
    if (!commandChanged && lastEffectAtMs && nowMs - lastEffectAtMs < interval) {
      return false;
    }
    lastEffectAtMs = nowMs;
    if (command.kind === "dodge") {
      state.dodges += 1;
      state.bullets = Math.max(0, state.bullets - 8);
      state.danger = Math.max(0, state.danger - 12);
      $("player").classList.add("dodge");
      setTimeout(() => $("player").classList.remove("dodge"), 220);
    } else if (command.kind === "fire") {
      state.shots += 1;
      state.enemies = Math.max(0, state.enemies - 1);
      state.danger = Math.min(100, state.danger + 3);
      $("laser").classList.add("firing");
      setTimeout(() => $("laser").classList.remove("firing"), 260);
    } else if (command.kind === "follow") {
      state.follows += 1;
      state.danger = Math.max(0, state.danger - 4);
    } else if (command.kind === "collect") {
      state.collected += 1;
      state.rewards = Math.max(0, state.rewards - 1);
      state.danger = Math.min(100, state.danger + 5);
    } else {
      return false;
    }
    return true;
  }

  function controllerApply({command, context}) {
    if (!["dodge", "fire", "follow", "collect"].includes(command.kind)) {
      return {accepted: false, reason: "unknown command"};
    }
    const commandChanged = state.lastCommand !== command.kind;
    state.controllerFrames += 1;
    state.lastCommand = command.kind;
    controlIntent = {kind: command.kind, targetX: targetForCommand(command)};
    const outcomeApplied = applySemanticOutcome(
      command,
      context.nowMs,
      commandChanged
    );
    render(outcomeApplied);
    return {
      accepted: true,
      effects: {
        command: command.kind,
        outcome: controllerOutcome(command.kind),
        outcomeApplied,
        field: semanticField(),
      },
    };
  }

  function policySummary({policy: activePolicy}) {
    return `Combat policy: ${activePolicy.mode}`;
  }

  function controllerOutcome(command) {
    const outcomes = {
      dodge: "pressure_evaded",
      fire: "attack_committed",
      follow: "player_following_started",
      collect: "reward_collected",
    };
    return outcomes[command] || "controller_effect_applied";
  }

  function onControllerStatus(status) {
    controllerStatus = status;
    render();
  }

  function clearControllerIntent() {
    policyEffectReported = false;
    lastPublishedCommand = "";
    lastEffectAtMs = 0;
    controlIntent = {kind: "idle", targetX: state.playerX};
  }

  function advanceBattle({forceCheckpoint = false} = {}) {
    if (!auip || !policy || controllerStatus.status !== "active") {
      state.danger = Math.min(100, state.danger + 2);
      render();
      return;
    }
    const nowMs = Date.now();
    const result = auip.controllerStep();
    if (!result || result.accepted !== true) return;
    const effect = result && result.effects ? result.effects : {};
    const command = effect.command || "none";
    const firstEffect = !policyEffectReported;
    const commandChanged = Boolean(
      policyEffectReported && command !== lastPublishedCommand
    );
    const meaningfulEffect = effect.outcomeApplied === true;
    if (meaningfulEffect) policyEffectReported = true;
    if (firstEffect || commandChanged) lastPublishedCommand = command;
    const semanticBoundary = Boolean(
      forceCheckpoint
      || (firstEffect && meaningfulEffect)
      || commandChanged
    );
    const envelope = semanticBoundary
      ? auip.checkpoint({
          actor: "app",
          effects: {
            command,
            outcome: effect.outcome,
            field: effect.field,
          },
          events: (firstEffect && meaningfulEffect) || commandChanged ? [{
            type: firstEffect
              ? "battle.controller_milestone"
              : "battle.controller_effect",
            actor: "app",
            payload: {
              mode: policy.mode,
              command,
              outcome: controllerOutcome(command),
            },
          }] : [],
        })
      : auip.checkpointIfDue({minimumIntervalMs: CHECKPOINT_INTERVAL_MS});
    envelope.publication.catch(() => {});
    return result;
  }

  const actions = {
    "battle.set_tactics": (payload, tx) => tx.commit({
      mutate: () => {
        policy = {mode: payload.mode};
        policyEffectReported = false;
        lastPublishedCommand = "";
        lastEffectAtMs = 0;
        controlIntent = {kind: "idle", targetX: state.playerX};
        render();
        return policy;
      },
      effects: ({result}) => ({mode: result.mode}),
      events: ({result}) => [{
        type: "battle.tactics_set",
        actor: "kurisu",
        payload: {mode: result.mode},
      }],
    }),
  };

  $("danger-wave").addEventListener("click", () => localWaveChange("danger"));
  $("calm-wave").addEventListener("click", () => localWaveChange("calm"));
  $("follow-scene").addEventListener("click", () => localWaveChange("follow"));
  $("reward-scene").addEventListener("click", () => localWaveChange("rewards"));
  $("advance-tick").addEventListener("click", () => {
    advanceBattle({forceCheckpoint: true});
  });

  // Policy decisions and render/physics deliberately use separate local
  // clocks. AUIP sets the lease and policy; it never drives individual frames.
  setInterval(() => {
    if (auip && policy && controllerStatus.status === "active") advanceBattle();
  }, CONTROL_TICK_MS);

  function animate(nowMs) {
    const elapsed = lastAnimationAtMs
      ? Math.min(0.05, Math.max(0, (nowMs - lastAnimationAtMs) / 1000))
      : 0;
    lastAnimationAtMs = nowMs;
    if (controlIntent.kind !== "idle") {
      const speed = controlIntent.kind === "dodge" ? 95 : 52;
      const delta = controlIntent.targetX - state.playerX;
      const distance = Math.min(Math.abs(delta), speed * elapsed);
      if (distance > 0) state.playerX += Math.sign(delta) * distance;
      renderMotion();
    }
    requestAnimationFrame(animate);
  }
  requestAnimationFrame(animate);

  render();
  if (
    typeof AmadeusAUIP !== "undefined"
    && typeof AmadeusAUIPSituations !== "undefined"
  ) {
    auip = AmadeusAUIP.createManagedApp({
      manifest,
      snapshot,
      initialEvents: [{type: "battle.ready", actor: "app", payload: {wave: state.wave}}],
      actions,
      onConnected: () => {
        connected = true;
        render();
      },
      controller: {
        observe: controllerObserve,
        decide: controllerDecide,
        apply: controllerApply,
        clearIntent: clearControllerIntent,
        policySummary,
        onStatus: onControllerStatus,
      },
    });
    auip.start().catch(() => {
      connected = false;
      render();
    });
  }

  window.__bulletHell = Object.freeze({
    snapshot: () => snapshot({controller: controllerStatus}),
    state: () => ({...state, policy: policy ? {...policy} : null}),
    setWave: localWaveChange,
    advance: () => advanceBattle({forceCheckpoint: true}),
    controllerStatus: () => auip ? auip.controllerStatus() : null,
  });
}());
