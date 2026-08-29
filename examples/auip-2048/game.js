(function () {
  "use strict";

  const SIZE = 4;
  const TARGET = 128;
  const TILE_SYMBOLS = "ABCDEFG";
  const TILE_LEGEND = Object.freeze({
    A: "2", B: "4", C: "8", D: "16", E: "32", F: "64", G: "128",
  });
  const DIRECTIONS = Object.freeze(["left", "right", "up", "down"]);
  const manifest = JSON.parse(document.getElementById("auip-manifest").textContent);
  const boardElement = document.getElementById("board");
  const scoreElement = document.getElementById("score");
  const statusElement = document.getElementById("status");
  const connectionElement = document.getElementById("connection");
  const cells = [];
  let grid = [2, 2, 4, 0, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
  let score = 0;
  let moveCount = 0;
  let status = "playing";
  let lastMove = null;
  let attached = false;
  let auip = null;

  function snapshot() {
    const legal = new Set(status === "playing" ? legalDirections() : []);
    return {
      board: window.AmadeusAUIPSituations.gridSituation({
        width: SIZE,
        height: SIZE,
        empty: ".",
        legend: TILE_LEGEND,
        cell: (x, y) => {
          const value = grid[y * SIZE + x];
          return value ? TILE_SYMBOLS[Math.log2(value) - 1] : ".";
        },
      }),
      actions: window.AmadeusAUIPSituations.choiceSituation({
        actionTypes: ["game.slide"],
        options: DIRECTIONS.map(direction => ({
          id: `slide-${direction}`,
          label: `Slide ${direction}`,
          action: "game.slide",
          payload: {direction},
          available: legal.has(direction),
        })),
      }),
      score,
      moveCount,
      highestTile: Math.max(...grid),
      status,
      lastMove: lastMove ? {...lastMove} : null,
    };
  }

  function legalDirections() {
    return DIRECTIONS.filter(direction => {
      for (let lane = 0; lane < SIZE; lane += 1) {
        const indices = lineIndices(direction, lane);
        const result = collapse(indices.map(index => grid[index]));
        if (indices.some((index, offset) => grid[index] !== result.values[offset])) {
          return true;
        }
      }
      return false;
    });
  }

  function lineIndices(direction, lane) {
    const result = [];
    for (let offset = 0; offset < SIZE; offset += 1) {
      if (direction === "left") result.push(lane * SIZE + offset);
      if (direction === "right") result.push(lane * SIZE + (SIZE - 1 - offset));
      if (direction === "up") result.push(offset * SIZE + lane);
      if (direction === "down") result.push((SIZE - 1 - offset) * SIZE + lane);
    }
    return result;
  }

  function collapse(values) {
    const compact = values.filter(Boolean);
    const merged = [];
    let gained = 0;
    let merges = 0;
    for (let index = 0; index < compact.length; index += 1) {
      if (compact[index] === compact[index + 1]) {
        const value = compact[index] * 2;
        merged.push(value);
        gained += value;
        merges += 1;
        index += 1;
      } else {
        merged.push(compact[index]);
      }
    }
    while (merged.length < SIZE) merged.push(0);
    return {values: merged, gained, merges};
  }

  function hasLegalMove() {
    if (grid.some(value => value === 0)) return true;
    for (let y = 0; y < SIZE; y += 1) {
      for (let x = 0; x < SIZE; x += 1) {
        const value = grid[y * SIZE + x];
        if (x + 1 < SIZE && grid[y * SIZE + x + 1] === value) return true;
        if (y + 1 < SIZE && grid[(y + 1) * SIZE + x] === value) return true;
      }
    }
    return false;
  }

  function spawnTile() {
    const empty = grid.map((value, index) => value === 0 ? index : -1).filter(index => index >= 0);
    if (!empty.length) return;
    const selected = empty[(moveCount * 5 + 3) % empty.length];
    grid[selected] = moveCount % 5 === 0 ? 4 : 2;
  }

  function commitSlide(direction, actor) {
    if (!["up", "down", "left", "right"].includes(direction)) {
      return {ok: false, reason: "invalid direction"};
    }
    if (status !== "playing") return {ok: false, reason: "game already finished"};
    const before = grid.slice();
    let gained = 0;
    let merges = 0;
    for (let lane = 0; lane < SIZE; lane += 1) {
      const indices = lineIndices(direction, lane);
      const result = collapse(indices.map(index => grid[index]));
      gained += result.gained;
      merges += result.merges;
      indices.forEach((index, offset) => { grid[index] = result.values[offset]; });
    }
    if (before.every((value, index) => value === grid[index])) {
      return {ok: false, reason: "slide does not change the board"};
    }
    score += gained;
    moveCount += 1;
    spawnTile();
    if (Math.max(...grid) >= TARGET) status = "won";
    else if (!hasLegalMove()) status = "over";
    lastMove = {direction, actor, gained, merges};
    render();
    return {ok: true, direction, actor, gained, merges, terminal: status !== "playing"};
  }

  function render() {
    cells.forEach((cell, index) => {
      const value = grid[index];
      cell.dataset.value = String(value);
      cell.textContent = value ? String(value) : "";
    });
    scoreElement.textContent = String(score);
    statusElement.textContent = status === "won" ? `Target ${TARGET} reached` : status === "over" ? "No legal slides" : `Move ${moveCount}`;
    connectionElement.textContent = attached
      ? `Attached · revision ${auip ? auip.revision() : 0}`
      : "Standalone mode";
  }

  function semanticEvent(move, actor) {
    return {
      type: "game.slide_committed",
      actor,
      payload: {
        direction: move.direction,
        scoreGained: move.gained,
        mergedTiles: move.merges,
        highestTile: Math.max(...grid),
        moveCount,
      },
    };
  }

  function terminalEvent(actor) {
    return {type: "game.finished", actor, payload: {outcome: status, score, highestTile: Math.max(...grid), moveCount}};
  }

  function slideEvents(move, actor) {
    return [
      semanticEvent(move, actor),
      ...(move.terminal ? [terminalEvent(actor)] : []),
    ];
  }

  function commitUserSlide(direction) {
    if (!auip) return commitSlide(direction, "user");
    const committed = auip.commitLocal({
      actor: "user",
      mutate: () => commitSlide(direction, "user"),
      effects: ({result, state}) => ({
        slide: {
          direction: result.direction,
          scoreGained: result.gained,
          mergedTiles: result.merges,
          highestTile: state.highestTile,
          label: `slide ${result.direction}`,
        },
      }),
      events: ({result}) => slideEvents(result, "user"),
    });
    render();
    committed.publication.catch(error => console.error("AUIP slide publication failed", error));
    return committed;
  }

  for (let index = 0; index < SIZE * SIZE; index += 1) {
    const cell = document.createElement("div");
    cell.className = "tile";
    cell.dataset.value = "0";
    boardElement.appendChild(cell);
    cells.push(cell);
  }

  document.querySelectorAll("button[data-direction]").forEach(button => {
    button.addEventListener("click", () => {
      commitUserSlide(button.dataset.direction);
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
      initialEvents: [{type: "game.ready", actor: "app", payload: {target: TARGET}}],
      actions: {
        "game.slide": (payload, tx) => {
          const outcome = tx.commit({
            mutate: () => commitSlide(String(payload.direction || ""), "kurisu"),
            effects: ({result, state}) => ({
              slide: {
                direction: result.direction,
                scoreGained: result.gained,
                mergedTiles: result.merges,
                highestTile: state.highestTile,
                label: `slide ${result.direction}`,
              },
            }),
            events: ({result}) => slideEvents(result, "kurisu"),
          });
          render();
          return outcome;
        },
      },
    });
    if (!launch) return;
    try {
      await auip.start();
      attached = true;
      render();
    } catch (_error) {
      attached = false;
      render();
    }
  }

  window.__auip2048 = {
    snapshot,
    isAttached: () => attached,
    revision: () => auip ? auip.revision() : 0,
    legalDirections,
    async close(reason) {
      if (!auip) return {ok: true};
      const result = await auip.close(reason || "app_closed");
      attached = false;
      render();
      return result;
    },
  };

  render();
  void attach();
})();
