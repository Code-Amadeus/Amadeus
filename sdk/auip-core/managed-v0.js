(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AmadeusAUIPManaged = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const DEFAULT_MAX_STATE_BYTES = 64 * 1024;
  const DEFAULT_PROJECTION_BUDGET_CHARS = 720;
  const ACTORS = new Set(["app", "user", "kurisu", "system"]);

  class ManagedCommitError extends Error {
    constructor(code, detail, options) {
      const cleanCode = String(code || "managed_commit_error");
      const cleanDetail = String(detail || "");
      super(cleanDetail ? `${cleanCode}: ${cleanDetail}` : cleanCode);
      this.name = "ManagedCommitError";
      this.code = cleanCode;
      this.detail = cleanDetail;
      this.afterMutation = Boolean(options && options.afterMutation);
    }
  }

  function createManagedCore(options) {
    const config = options || {};
    const manifest = deepFreeze(cloneJson(config.manifest, "manifest"));
    const snapshotReader = typeof config.snapshot === "function"
      ? config.snapshot
      : typeof config.getState === "function"
        ? config.getState
        : null;
    if (!snapshotReader) {
      throw new ManagedCommitError("snapshot_required");
    }
    if (isAsyncFunction(snapshotReader)) {
      throw new ManagedCommitError("async_snapshot_not_allowed");
    }
    const handlers = config.actions && typeof config.actions === "object"
      ? config.actions
      : {};
    const declaredActions = manifest.actions && typeof manifest.actions === "object"
      ? Object.keys(manifest.actions).sort()
      : [];
    const handlerActions = Object.keys(handlers).sort();
    const declaredEvents = new Set(
      manifest.events && typeof manifest.events === "object"
        ? Object.keys(manifest.events)
        : []
    );
    assertActionParity(declaredActions, handlerActions);
    handlerActions.forEach(function (actionType) {
      if (typeof handlers[actionType] !== "function") {
        throw new ManagedCommitError("invalid_action_handler", actionType);
      }
      if (isAsyncFunction(handlers[actionType])) {
        throw new ManagedCommitError("async_action_handler_not_allowed", actionType);
      }
    });

    const maxStateBytes = positiveInteger(
      config.maxStateBytes,
      DEFAULT_MAX_STATE_BYTES
    );
    const projectionBudgetChars = positiveInteger(
      config.projectionBudgetChars,
      DEFAULT_PROJECTION_BUDGET_CHARS
    );
    const onDiagnostic = typeof config.onDiagnostic === "function"
      ? config.onDiagnostic
      : function () {};
    let revision = nonNegativeInteger(config.initialRevision, 0);
    let poisoned = false;
    let acceptedSnapshotJson = "";

    function diagnostic(payload) {
      try {
        onDiagnostic(deepFreeze(cloneJson(payload, "diagnostic")));
      } catch (_error) {
        // Diagnostics are presentation only and never alter commit authority.
      }
    }

    function ensureHealthy() {
      if (poisoned) {
        throw new ManagedCommitError("managed_core_desynchronized");
      }
    }

    function freezeSnapshot(context) {
      let value;
      try {
        value = snapshotReader(context || {revision: revision});
      } catch (error) {
        throw managedError("snapshot_failed", error);
      }
      if (isPromiseLike(value)) {
        throw new ManagedCommitError("async_snapshot_not_allowed");
      }
      const serialized = stringifyJson(value, "state");
      const bytes = utf8Bytes(serialized);
      if (bytes > maxStateBytes) {
        throw new ManagedCommitError(
          "state_size_exceeded",
          `${bytes} > ${maxStateBytes}`
        );
      }
      if (serialized.length > projectionBudgetChars) {
        diagnostic({
          code: "projection_budget_exceeded",
          revision: Number(context && context.revision || revision),
          characters: serialized.length,
          recommendedMaximum: projectionBudgetChars,
        });
      }
      return deepFreeze(JSON.parse(serialized));
    }

    function reject(reason, code) {
      return deepFreeze({
        committed: false,
        accepted: false,
        revision: revision,
        reason: cleanReason(reason),
        code: String(code || "action_rejected"),
      });
    }

    function commitSpec(specification, defaultActor, allowKurisu) {
      ensureHealthy();
      const spec = specification && typeof specification === "object"
        ? specification
        : {};
      const actor = normalizeActor(spec.actor || defaultActor || "app");
      if (actor === "kurisu" && allowKurisu !== true) {
        throw new ManagedCommitError("local_kurisu_authority_forbidden");
      }
      const mutate = spec.mutate;
      if (mutate !== undefined && typeof mutate !== "function") {
        throw new ManagedCommitError("invalid_mutation");
      }
      if (isAsyncFunction(mutate)) {
        throw new ManagedCommitError("async_mutation_not_allowed");
      }

      let mutationResult = {};
      let mutationSucceeded = false;
      try {
        if (mutate) mutationResult = mutate();
        if (isPromiseLike(mutationResult)) {
          throw new ManagedCommitError("async_mutation_not_allowed");
        }
        if (
          mutationResult
          && typeof mutationResult === "object"
          && (mutationResult.ok === false || mutationResult.accepted === false)
        ) {
          return reject(
            mutationResult.reason || "application rejected the transition",
            mutationResult.code
          );
        }
        mutationSucceeded = Boolean(mutate);
        const nextRevision = revision + 1;
        const state = freezeSnapshot({
          revision: nextRevision,
          actor: actor,
          result: mutationResult,
        });
        const descriptorContext = deepFreeze({
          revision: nextRevision,
          actor: actor,
          result: cloneJson(mutationResult || {}, "mutation result"),
          state: state,
        });
        const effects = resolveDescriptor(
          spec.effects,
          descriptorContext,
          {},
          "effects"
        );
        const events = normalizeEvents(
          resolveDescriptor(spec.events, descriptorContext, [], "events"),
          actor,
          declaredEvents,
          allowKurisu === true
        );
        revision = nextRevision;
        acceptedSnapshotJson = stringifyJson(state, "accepted state");
        return deepFreeze({
          committed: true,
          accepted: true,
          revision: revision,
          state: state,
          effects: effects,
          events: events,
        });
      } catch (error) {
        if (mutationSucceeded) {
          poisoned = true;
          diagnostic({
            code: "post_mutation_projection_failed",
            revision: revision,
            reason: error && error.message ? error.message : String(error),
          });
          const wrapped = new ManagedCommitError(
            "post_mutation_projection_failed",
            error && error.message ? error.message : String(error || ""),
            {afterMutation: true}
          );
          throw wrapped;
        }
        throw error;
      }
    }

    function dispatchAction(rawAction) {
      ensureHealthy();
      const action = rawAction && typeof rawAction === "object" ? rawAction : {};
      const actionType = String(action.type || "").trim();
      const expectedRevision = Number(action.expected_revision);
      if (!Number.isInteger(expectedRevision) || expectedRevision !== revision) {
        return reject(
          `expected revision ${expectedRevision} does not match ${revision}`,
          "stale_action_revision"
        );
      }
      const handler = handlers[actionType];
      if (typeof handler !== "function") {
        return reject(`undeclared action ${actionType}`, "undeclared_action");
      }
      const payload = deepFreeze(cloneJson(action.payload || {}, "action payload"));
      const acceptedState = freezeSnapshot({revision: revision, actor: "kurisu"});
      const currentSnapshotJson = stringifyJson(acceptedState, "accepted state");
      if (currentSnapshotJson !== acceptedSnapshotJson) {
        diagnostic({
          code: "state_changed_without_checkpoint",
          revision: revision,
          actionType: actionType,
        });
        return reject(
          "action-relevant state changed without commitLocal or checkpoint",
          "state_changed_without_checkpoint"
        );
      }
      let conclusion = null;
      let concluded = false;
      const transaction = Object.freeze({
        revision: revision,
        state: acceptedState,
        action: deepFreeze(cloneJson(action, "action")),
        commit(spec) {
          if (concluded) {
            throw new ManagedCommitError("action_already_concluded", actionType);
          }
          concluded = true;
          conclusion = commitSpec(spec, "kurisu", true);
          return conclusion;
        },
        reject(reason, code) {
          if (concluded) {
            throw new ManagedCommitError("action_already_concluded", actionType);
          }
          concluded = true;
          conclusion = reject(reason, code);
          return conclusion;
        },
      });
      let returned;
      try {
        returned = handler(payload, transaction);
      } catch (error) {
        if (error && (error.afterMutation === true || poisoned)) throw error;
        if (conclusion && conclusion.committed) {
          diagnostic({
            code: "action_handler_failed_after_commit",
            revision: conclusion.revision,
            actionType: actionType,
            reason: error && error.message ? error.message : String(error),
          });
          return conclusion;
        }
        return reject(
          error && error.message ? error.message : String(error),
          "action_handler_failed"
        );
      }
      if (isPromiseLike(returned)) {
        if (conclusion && conclusion.committed) {
          poisoned = true;
          diagnostic({
            code: "async_action_handler_after_commit",
            revision: conclusion.revision,
            actionType: actionType,
          });
          return conclusion;
        }
        return reject(
          "managed action handlers must conclude synchronously",
          "async_action_handler_not_allowed"
        );
      }
      if (conclusion) return conclusion;
      return reject(
        "managed action handler returned without commit or reject",
        "action_not_concluded"
      );
    }

    // Validate the initial projection while no mutation can yet have occurred.
    acceptedSnapshotJson = stringifyJson(
      freezeSnapshot({revision: revision, actor: "app"}),
      "accepted state"
    );

    return Object.freeze({
      manifest: manifest,
      revision() { return revision; },
      snapshot() {
        ensureHealthy();
        return freezeSnapshot({revision: revision, actor: "app"});
      },
      commitLocal(specification) {
        return commitSpec(specification, "user", false);
      },
      checkpoint(specification) {
        const spec = Object.assign({}, specification || {});
        delete spec.mutate;
        return commitSpec(spec, spec.actor || "app", false);
      },
      checkpointIfChanged(specification) {
        ensureHealthy();
        const spec = Object.assign({}, specification || {});
        delete spec.mutate;
        const actor = normalizeActor(spec.actor || "app");
        const currentState = freezeSnapshot({revision: revision, actor: actor});
        const currentSnapshotJson = stringifyJson(currentState, "current state");
        if (currentSnapshotJson === acceptedSnapshotJson) {
          return deepFreeze({
            committed: false,
            revision: revision,
            code: "checkpoint_unchanged",
          });
        }
        return commitSpec(spec, actor, false);
      },
      dispatchAction: dispatchAction,
      healthy() { return !poisoned; },
    });
  }

  function assertActionParity(declared, handlers) {
    const missing = declared.filter(function (name) { return !handlers.includes(name); });
    const extra = handlers.filter(function (name) { return !declared.includes(name); });
    if (missing.length || extra.length) {
      throw new ManagedCommitError(
        "manifest_handler_mismatch",
        `missing=[${missing.join(",")}] extra=[${extra.join(",")}]`
      );
    }
  }

  function normalizeEvents(value, defaultActor, declaredEvents, allowKurisu) {
    if (!Array.isArray(value)) {
      throw new ManagedCommitError("invalid_events", "events must be an array");
    }
    if (value.length > 4) {
      throw new ManagedCommitError("too_many_events", String(value.length));
    }
    return deepFreeze(value.map(function (rawEvent) {
      const event = rawEvent && typeof rawEvent === "object" ? rawEvent : {};
      const type = String(event.type || "").trim();
      if (!declaredEvents.has(type)) {
        throw new ManagedCommitError("undeclared_event", type);
      }
      const actor = normalizeActor(event.actor || defaultActor);
      if (actor === "kurisu" && allowKurisu !== true) {
        throw new ManagedCommitError("local_kurisu_authority_forbidden", type);
      }
      const normalized = {
        type: type,
        actor: actor,
        payload: cloneJson(event.payload || {}, "event payload"),
      };
      if (event.eventId !== undefined) {
        normalized.eventId = String(event.eventId || "");
      }
      return normalized;
    }));
  }

  function resolveDescriptor(value, context, fallback, name) {
    let resolved = value === undefined ? fallback : value;
    if (typeof resolved === "function") resolved = resolved(context);
    if (isPromiseLike(resolved)) {
      throw new ManagedCommitError(`async_${name}_not_allowed`);
    }
    return deepFreeze(cloneJson(resolved, name));
  }

  function normalizeActor(value) {
    const actor = String(value || "").trim().toLowerCase();
    if (!ACTORS.has(actor)) {
      throw new ManagedCommitError("invalid_actor", actor);
    }
    return actor;
  }

  function cloneJson(value, name) {
    return JSON.parse(stringifyJson(value, name));
  }

  function stringifyJson(value, name) {
    let serialized;
    try {
      serialized = JSON.stringify(value);
    } catch (error) {
      throw managedError(`invalid_${String(name || "value").replace(/\s+/g, "_")}`, error);
    }
    if (serialized === undefined) {
      throw new ManagedCommitError(
        `invalid_${String(name || "value").replace(/\s+/g, "_")}`
      );
    }
    return serialized;
  }

  function deepFreeze(value) {
    if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
    Object.keys(value).forEach(function (key) { deepFreeze(value[key]); });
    return Object.freeze(value);
  }

  function cleanReason(value) {
    const reason = String(value || "application rejected the transition")
      .replace(/\s+/g, " ")
      .trim();
    return reason.slice(0, 600) || "application rejected the transition";
  }

  function isPromiseLike(value) {
    return Boolean(value && typeof value.then === "function");
  }

  function isAsyncFunction(value) {
    return Boolean(
      typeof value === "function"
      && value.constructor
      && value.constructor.name === "AsyncFunction"
    );
  }

  function positiveInteger(value, fallback) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
  }

  function nonNegativeInteger(value, fallback) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : fallback;
  }

  function utf8Bytes(value) {
    if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(value).length;
    if (typeof Buffer !== "undefined") return Buffer.byteLength(value, "utf8");
    return unescape(encodeURIComponent(value)).length;
  }

  function managedError(code, error, options) {
    if (error instanceof ManagedCommitError) return error;
    return new ManagedCommitError(
      code,
      error && error.message ? error.message : String(error || ""),
      options
    );
  }

  return {
    createManagedCore: createManagedCore,
    ManagedCommitError: ManagedCommitError,
  };
});
