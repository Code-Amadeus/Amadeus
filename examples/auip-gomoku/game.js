(function () {
  "use strict";

  const SIZE = 9;
  const WIN_LENGTH = 5;
  const EMPTY = 0;
  const BLACK = 1;
  const WHITE = 2;
  const boardElement = document.getElementById("board");
  const statusElement = document.getElementById("status");
  const connectionElement = document.getElementById("connection");
  const moveCountElement = document.getElementById("move-count");
  const revisionElement = document.getElementById("revision");
  const resetButton = document.getElementById("reset");
  const finishButton = document.getElementById("finish");
  const manifest = JSON.parse(document.getElementById("auip-manifest").textContent);
  const board = new Array(SIZE * SIZE).fill(EMPTY);
  const cells = [];
  let turn = BLACK;
  let winner = EMPTY;
  let draw = false;
  let lastMove = null;
  let moveCount = 0;
  let attached = false;
  let experienceConcluded = false;
  let finishReason = "";
  let auip = null;
  let participantSide = WHITE;
  let userSide = BLACK;

  function sideName(side) {
    return side === BLACK ? "black" : side === WHITE ? "white" : "none";
  }

  function indexOf(x, y) {
    return y * SIZE + x;
  }

  function inside(x, y) {
    return Number.isInteger(x) && Number.isInteger(y)
      && x >= 0 && y >= 0 && x < SIZE && y < SIZE;
  }

  function snapshot() {
    const roundFinished = Boolean(winner || draw);
    const lifecycle = experienceConcluded
      ? "concluded"
      : roundFinished ? "round_finished" : "playing";
    const lifecycleOptions = [
      {
        id: "s",
        label: `switch participant to ${participantSide === BLACK ? "white" : "black"}`,
        action: "game.configure_participants",
        payload: {participantSide: participantSide === BLACK ? "white" : "black"},
        available: lifecycle === "playing" && moveCount === 0,
      },
      {
        id: "r",
        label: "resign",
        action: "game.resign",
        payload: {},
        available: lifecycle === "playing",
      },
      {
        id: "n",
        label: "restart",
        action: "game.restart_round",
        payload: {},
        available: lifecycle === "round_finished",
      },
      {
        id: "x",
        label: "finish",
        action: "game.finish_experience",
        payload: {},
        available: lifecycle === "round_finished",
      },
    ];
    const availableGridActions = [];
    if (lifecycle === "playing" && turn === participantSide) {
      availableGridActions.push("game.place_stone");
    }
    if (
      lifecycle === "playing"
      && moveCount === 0
      && turn === BLACK
      && participantSide !== BLACK
    ) {
      availableGridActions.push("game.take_first_move");
    }
    return {
      board: window.AmadeusAUIPSituations.gridSituation({
        width: SIZE,
        height: SIZE,
        empty: ".",
        legend: {B: "black", W: "white"},
        cell: (x, y) => board[indexOf(x, y)] === BLACK
          ? "B"
          : board[indexOf(x, y)] === WHITE ? "W" : ".",
      }),
      turn: winner || draw ? "none" : sideName(turn),
      winner: winner ? sideName(winner) : draw ? "draw" : "none",
      lifecycle,
      finishReason: finishReason || "none",
      actionAvailability: window.AmadeusAUIPSituations.actionAvailabilitySituation({
        actionTypes: ["game.place_stone", "game.take_first_move"],
        availableActionTypes: availableGridActions,
      }),
      actions: window.AmadeusAUIPSituations.choiceSituation({
        actionAddressed: true,
        actionTypes: [
          "game.configure_participants",
          "game.resign",
          "game.restart_round",
          "game.finish_experience",
        ],
        options: lifecycleOptions,
      }),
      moveCount,
      lastMove: lastMove ? {x: lastMove.x, y: lastMove.y, side: sideName(lastMove.side)} : null,
      roleBindings: {
        user: sideName(userSide),
        participant: sideName(participantSide),
      },
    };
  }

  function countDirection(x, y, dx, dy, side) {
    let total = 0;
    let nextX = x + dx;
    let nextY = y + dy;
    while (inside(nextX, nextY) && board[indexOf(nextX, nextY)] === side) {
      total += 1;
      nextX += dx;
      nextY += dy;
    }
    return total;
  }

  function hasFive(x, y, side) {
    return [[1, 0], [0, 1], [1, 1], [1, -1]].some(([dx, dy]) => (
      1
      + countDirection(x, y, dx, dy, side)
      + countDirection(x, y, -dx, -dy, side)
    ) >= WIN_LENGTH);
  }

  function render() {
    cells.forEach((cell, index) => {
      const value = board[index];
      cell.className = "cell";
      if (value === BLACK) cell.classList.add("black");
      if (value === WHITE) cell.classList.add("white");
      if (lastMove && index === indexOf(lastMove.x, lastMove.y)) cell.classList.add("last");
      cell.disabled = Boolean(
        winner
        || draw
        || value !== EMPTY
        || (attached && turn !== userSide)
      );
      cell.setAttribute("aria-label", `${cell.dataset.x},${cell.dataset.y}: ${sideName(value)}`);
    });
    statusElement.textContent = experienceConcluded
      ? "Series finished"
      : winner
      ? `${sideName(winner)[0].toUpperCase()}${sideName(winner).slice(1)} wins`
      : draw
        ? "Draw"
        : `${sideName(turn)[0].toUpperCase()}${sideName(turn).slice(1)} to move`;
    connectionElement.textContent = attached ? "Attached to Amadeus" : "Standalone mode";
    moveCountElement.textContent = String(moveCount);
    revisionElement.textContent = String(auip ? auip.revision() : 0);
    resetButton.disabled = attached && experienceConcluded;
    finishButton.disabled = experienceConcluded || !(winner || draw);
  }

  function commitMove(x, y, actor) {
    if (!inside(x, y)) return {ok: false, reason: "coordinates out of range"};
    if (winner || draw) return {ok: false, reason: "match already finished"};
    const index = indexOf(x, y);
    if (board[index] !== EMPTY) return {ok: false, reason: "intersection occupied"};
    const side = turn;
    board[index] = side;
    moveCount += 1;
    lastMove = {x, y, side, actor};
    if (hasFive(x, y, side)) {
      winner = side;
      finishReason = "five_in_a_row";
    } else if (moveCount === board.length) {
      draw = true;
      finishReason = "draw";
    }
    else turn = side === BLACK ? WHITE : BLACK;
    render();
    return {ok: true, x, y, side: sideName(side), actor, terminal: Boolean(winner || draw)};
  }

  function resetMatch() {
    board.fill(EMPTY);
    turn = BLACK;
    winner = EMPTY;
    draw = false;
    lastMove = null;
    moveCount = 0;
    experienceConcluded = false;
    finishReason = "";
    render();
  }

  function resignParticipant() {
    if (experienceConcluded || winner || draw) {
      return {ok: false, reason: "round is not active"};
    }
    winner = userSide;
    finishReason = "participant_resigned";
    render();
    return {ok: true, winner: sideName(winner), reason: finishReason};
  }

  function concludeExperience() {
    if (experienceConcluded || (!winner && !draw)) {
      return {ok: false, reason: "experience can conclude only after a round result"};
    }
    experienceConcluded = true;
    render();
    return {ok: true, winner: snapshot().winner, reason: finishReason || "draw"};
  }

  function configureParticipantSide(side) {
    if (moveCount !== 0) return {ok: false, reason: "roles can change only before the first move"};
    const nextParticipantSide = side === "black" ? BLACK : side === "white" ? WHITE : EMPTY;
    if (!nextParticipantSide) return {ok: false, reason: "participantSide must be black or white"};
    if (nextParticipantSide === participantSide) {
      return {ok: false, reason: "participant already controls that side"};
    }
    participantSide = nextParticipantSide;
    userSide = participantSide === BLACK ? WHITE : BLACK;
    render();
    return {ok: true};
  }

  function moveEvent(move, actor) {
    return {
      type: "game.move_committed",
      actor,
      payload: {x: move.x, y: move.y, side: move.side, moveCount},
    };
  }

  function roundFinishedEvent() {
    return {
      type: "game.round_finished",
      actor: "app",
      payload: {winner: snapshot().winner, moveCount, reason: finishReason || "draw"},
    };
  }

  function experienceFinishedEvent() {
    return {
      type: "game.experience_finished",
      actor: "app",
      payload: {winner: snapshot().winner, moveCount, reason: finishReason || "draw"},
    };
  }

  function participantTurnReadyEvent() {
    return {
      type: "game.participant_turn_ready",
      actor: "app",
      payload: {side: sideName(participantSide), moveCount},
    };
  }

  function moveEvents(move, actor) {
    const events = [moveEvent(move, actor)];
    if (move.terminal) events.push(roundFinishedEvent());
    else if (turn === participantSide) events.push(participantTurnReadyEvent());
    return events;
  }

  function commitUserMove(x, y) {
    if (!auip) return commitMove(x, y, "user");
    const committed = auip.commitLocal({
      actor: "user",
      mutate: () => {
        return commitMove(x, y, "user");
      },
      effects: ({result}) => ({
        placed: {x: result.x, y: result.y, side: result.side},
      }),
      events: ({result}) => moveEvents(result, "user"),
    });
    render();
    committed.publication.catch(error => console.error("AUIP move publication failed", error));
    return committed;
  }

  for (let y = 0; y < SIZE; y += 1) {
    for (let x = 0; x < SIZE; x += 1) {
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "cell";
      cell.dataset.x = String(x);
      cell.dataset.y = String(y);
      cell.setAttribute("role", "gridcell");
      cell.addEventListener("click", () => {
        if (attached && turn !== userSide) return;
        commitUserMove(x, y);
      });
      cells.push(cell);
      boardElement.appendChild(cell);
    }
  }

  resetButton.addEventListener("click", () => {
    if (attached && experienceConcluded) return;
    if (!auip) {
      resetMatch();
      return;
    }
    const committed = auip.commitLocal({
      actor: "user",
      mutate: () => {
        resetMatch();
        return {ok: true};
      },
      effects: {reset: true},
      events: () => [
        {type: "game.reset", actor: "user", payload: {}},
        ...(turn === participantSide ? [participantTurnReadyEvent()] : []),
      ],
    });
    render();
    committed.publication.catch(error => console.error("AUIP reset publication failed", error));
  });

  finishButton.addEventListener("click", () => {
    if (experienceConcluded || (!winner && !draw)) return;
    if (!auip) {
      concludeExperience();
      return;
    }
    const committed = auip.commitLocal({
      actor: "user",
      mutate: concludeExperience,
      effects: ({result}) => ({
        concluded: true,
        winner: result.winner,
        reason: result.reason,
      }),
      events: () => [experienceFinishedEvent()],
    });
    render();
    committed.publication.catch(error => console.error("AUIP series conclusion publication failed", error));
  });

  async function attach() {
    if (!window.AmadeusAUIP) return;
    auip = window.AmadeusAUIP.createManagedApp({
      manifest,
      snapshot,
      initialEvents: () => [
        {type: "game.ready", actor: "app", payload: {size: SIZE, winLength: WIN_LENGTH}},
        ...(turn === participantSide ? [participantTurnReadyEvent()] : []),
      ],
      actions: {
        "game.take_first_move": (payload, tx) => {
          const x = Number(payload.x);
          const y = Number(payload.y);
          if (
            moveCount !== 0
            || turn !== BLACK
            || participantSide === BLACK
            || winner
            || draw
          ) {
            return tx.reject(
              "the atomic first-move transition is not available",
              "first_move_not_available"
            );
          }
          if (!inside(x, y) || board[indexOf(x, y)] !== EMPTY) {
            return tx.reject("the requested first intersection is not empty", "illegal_move");
          }
          const outcome = tx.commit({
            mutate: () => {
              configureParticipantSide("black");
              return commitMove(x, y, "kurisu");
            },
            effects: ({result, state}) => ({
              roleBindings: state.roleBindings,
              placed: {x: result.x, y: result.y, side: result.side},
            }),
            events: ({result, state}) => [
              {
                type: "game.participants_configured",
                actor: "kurisu",
                payload: {roleBindings: state.roleBindings},
              },
              ...moveEvents(result, "kurisu"),
            ],
          });
          render();
          return outcome;
        },
        "game.configure_participants": (payload, tx) => {
          const outcome = tx.commit({
            mutate: () => configureParticipantSide(String(payload.participantSide || "")),
            effects: ({state}) => ({roleBindings: state.roleBindings}),
            events: ({state}) => [
              {
                type: "game.participants_configured",
                actor: "kurisu",
                payload: {roleBindings: state.roleBindings},
              },
              ...(state.turn === state.roleBindings.participant
                ? [participantTurnReadyEvent()]
                : []),
            ],
          });
          render();
          return outcome;
        },
        "game.place_stone": (payload, tx) => {
          if (turn !== participantSide) {
            return tx.reject("it is not the protocol participant's bound turn");
          }
          const outcome = tx.commit({
            mutate: () => {
              return commitMove(Number(payload.x), Number(payload.y), "kurisu");
            },
            effects: ({result}) => ({
              placed: {x: result.x, y: result.y, side: result.side},
            }),
            events: ({result}) => moveEvents(result, "kurisu"),
          });
          render();
          return outcome;
        },
        "game.resign": (_payload, tx) => {
          if (experienceConcluded || winner || draw) {
            return tx.reject("round is not active", "round_not_active");
          }
          const outcome = tx.commit({
            mutate: resignParticipant,
            effects: ({result}) => ({
              resigned: true,
              winner: result.winner,
            }),
            events: () => [roundFinishedEvent()],
          });
          render();
          return outcome;
        },
        "game.restart_round": (_payload, tx) => {
          if (!winner && !draw) {
            return tx.reject("round has not finished", "round_not_finished");
          }
          const outcome = tx.commit({
            mutate: () => {
              resetMatch();
              return {ok: true};
            },
            effects: {restarted: true},
            events: () => [
              {type: "game.reset", actor: "kurisu", payload: {}},
              ...(turn === participantSide ? [participantTurnReadyEvent()] : []),
            ],
          });
          render();
          return outcome;
        },
        "game.finish_experience": (_payload, tx) => {
          if (experienceConcluded || (!winner && !draw)) {
            return tx.reject(
              "match series can end only after a round result",
              "round_not_finished"
            );
          }
          const outcome = tx.commit({
            mutate: concludeExperience,
            effects: ({result}) => ({
              concluded: true,
              winner: result.winner,
              reason: result.reason,
            }),
            events: () => [experienceFinishedEvent()],
          });
          render();
          return outcome;
        },
      },
    });
    try {
      await auip.start();
      attached = true;
      render();
    } catch (_error) {
      attached = false;
      render();
    }
  }

  window.__auipGomoku = {
    snapshot,
    play(x, y) { return commitUserMove(Number(x), Number(y)); },
    isAttached() { return attached; },
    revision() { return auip ? auip.revision() : 0; },
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
