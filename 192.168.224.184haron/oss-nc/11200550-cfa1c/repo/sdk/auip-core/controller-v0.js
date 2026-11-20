(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AmadeusAUIPController = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PRINCIPAL = "kurisu";
  const EXECUTOR = "app_controller";
  const TAKEOVER = new Set(["immediate", "safe_point"]);

  class ControllerContractError extends Error {
    constructor(code, detail) {
      const cleanCode = String(code || "invalid_controller_contract");
      const cleanDetail = String(detail || "");
      super(cleanDetail ? `${cleanCode}: ${cleanDetail}` : cleanCode);
      this.name = "ControllerContractError";
      this.code = cleanCode;
      this.detail = cleanDetail;
    }
  }

  function createReactiveController(options) {
    const config = options && typeof options === "object" ? options : {};
    const observe = requiredSyncFunction(config.observe, "observe");
    const decide = requiredSyncFunction(config.decide, "decide");
    const apply = requiredSyncFunction(config.apply, "apply");
    const clearIntent = requiredSyncFunction(config.clearIntent, "clearIntent");
    const onStatus = typeof config.onStatus === "function"
      ? config.onStatus
      : function () {};
    if (isAsyncFunction(onStatus)) {
      throw new ControllerContractError("async_status_handler_not_allowed");
    }

    let current = null;
    let state = "idle";
    let statusReason = "";
    let highestGeneration = -1;
    let commandSequence = 0;
    let lastAppliedAtMs = null;

    function publicStatus() {
      const value = {
        kind: "controller/v1",
        status: state,
        policyRevision: current ? current.lease.policy_revision : null,
        policyAction: current ? current.actionType : null,
        policySummary: current ? current.policySummary : "",
      };
      if (statusReason) value.reason = statusReason;
      return deepFreeze(value);
    }

    function publishStatus() {
      const snapshot = publicStatus();
      try {
        const returned = onStatus(snapshot);
        if (isPromiseLike(returned)) {
          throw new ControllerContractError("async_status_handler_not_allowed");
        }
      } catch (_error) {
        // Status publication is presentation only and never grants authority.
      }
      return snapshot;
    }

    function transition(nextState, reason) {
      state = nextState;
      statusReason = boundedOptionalText(reason, "status reason", 160);
      return publishStatus();
    }

    function forgetCurrent() {
      current = null;
      lastAppliedAtMs = null;
      commandSequence = 0;
    }

    function releaseApplicationIntent(previous, reason) {
      if (!previous) return null;
      const cleanReason = boundedText(reason, "release reason", 160);
      try {
        const returned = clearIntent(deepFreeze({
          reason: cleanReason,
          lease: previous.lease,
          actionType: previous.actionType,
          policy: previous.policy,
        }));
        if (isPromiseLike(returned)) {
          throw new ControllerContractError("async_clear_intent_not_allowed");
        }
        return null;
      } catch (error) {
        return error instanceof ControllerContractError
          ? error
          : new ControllerContractError(
              "controller_clear_intent_failed",
              error && error.message ? error.message : String(error)
            );
      }
    }

    function deactivate(reason) {
      const previous = current;
      const releaseError = releaseApplicationIntent(previous, reason);
      forgetCurrent();
      if (releaseError) {
        const status = transition("blocked", releaseError.message);
        return {previous: previous, status: status, error: releaseError};
      }
      const status = transition("idle", reason);
      return {previous: previous, status: status, error: null};
    }

    function inspectActivationLease(rawLease) {
      const lease = normalizeLease(rawLease);
      if (lease.generation <= highestGeneration) {
        return refusal("stale_controller_lease", "controller generation is not newer");
      }
      return {accepted: true, lease: lease};
    }

    function canActivate(rawLease) {
      try {
        const inspected = inspectActivationLease(rawLease);
        if (inspected.accepted !== true) return inspected;
        return deepFreeze({
          accepted: true,
          code: "controller_lease_available",
          leaseId: inspected.lease.lease_id,
          generation: inspected.lease.generation,
        });
      } catch (error) {
        if (error instanceof ControllerContractError) {
          return refusal(error.code, error.detail || error.message);
        }
        return refusal(
          "invalid_controller_contract",
          error && error.message ? error.message : String(error)
        );
      }
    }

    function activate(rawActivation) {
      const activation = objectValue(rawActivation, "activation");
      const inspected = inspectActivationLease(activation.lease);
      if (inspected.accepted !== true) return inspected;
      const lease = inspected.lease;
      const actionType = semanticType(activation.actionType, "actionType");
      const policy = frozenObject(activation.policy, "policy");
      const policySummary = boundedText(
        activation.policySummary,
        "policySummary",
        240
      );
      const replacedLeaseId = current ? current.lease.lease_id : null;
      if (current) {
        const ended = deactivate("replaced");
        if (ended.error) {
          return refusal(ended.error.code, ended.error.detail || ended.error.message);
        }
      }
      current = deepFreeze({
        lease: lease,
        actionType: actionType,
        policy: policy,
        policySummary: policySummary,
      });
      highestGeneration = lease.generation;
      commandSequence = 0;
      lastAppliedAtMs = null;
      const status = transition("active", "");
      return deepFreeze({
        accepted: true,
        code: replacedLeaseId
          ? "controller_policy_replaced"
          : "controller_activated",
        replacedLeaseId: replacedLeaseId,
        status: status,
      });
    }

    function step(rawStep) {
      const request = rawStep === undefined ? {} : objectValue(rawStep, "step");
      const nowMs = timestamp(request.nowMs, "nowMs");
      const reconciled = reconcile({nowMs: nowMs});
      if (reconciled.code === "controller_lease_expired") {
        return refusal("controller_lease_expired", "controller lease expired");
      }
      if (reconciled.accepted !== true) return reconciled;
      if (state === "blocked") {
        return refusal("controller_blocked", statusReason || "controller is blocked");
      }
      if (!current || state === "idle") {
        return refusal("controller_inactive", statusReason || "controller is idle");
      }
      if (state === "stopping") {
        return refusal("controller_stopping", "safe-point takeover is pending");
      }
      const lease = current.lease;
      const context = deepFreeze({
        nowMs: nowMs,
        lease: lease,
        commandSequence: commandSequence + 1,
      });
      let observation;
      let command;
      try {
        observation = frozenJson(observe(context), "observation");
        command = decide(deepFreeze({
          observation: observation,
          policy: current.policy,
          context: context,
        }));
        if (isPromiseLike(command)) {
          throw new ControllerContractError("async_decision_not_allowed");
        }
      } catch (error) {
        return block("controller_decision_failed", error);
      }
      if (command === null || command === undefined) {
        return refusal("no_controller_command", "application policy chose no command");
      }
      const frozenCommand = frozenObject(command, "command");
      const minimumIntervalMs = 1000 / lease.max_action_rate_hz;
      if (
        lastAppliedAtMs !== null
        && nowMs - lastAppliedAtMs < minimumIntervalMs
      ) {
        return refusal("controller_rate_limited", "lease action-rate ceiling reached");
      }

      let applicationResult;
      try {
        applicationResult = apply(deepFreeze({
          command: frozenCommand,
          observation: observation,
          policy: current.policy,
          context: context,
        }));
        if (isPromiseLike(applicationResult)) {
          throw new ControllerContractError("async_apply_not_allowed");
        }
      } catch (error) {
        return block("controller_apply_failed", error);
      }
      const result = objectValue(applicationResult, "application result");
      if (result.accepted !== true) {
        return refusal(
          String(result.code || "controller_command_rejected"),
          result.reason || "application rejected controller command"
        );
      }
      commandSequence += 1;
      lastAppliedAtMs = nowMs;
      return deepFreeze({
        accepted: true,
        code: "controller_command_applied",
        leaseId: lease.lease_id,
        generation: lease.generation,
        commandSequence: commandSequence,
        command: frozenCommand,
        effects: frozenObject(result.effects || {}, "effects"),
        status: publicStatus(),
      });
    }

    function reconcile(rawRequest) {
      const request = rawRequest === undefined
        ? {}
        : objectValue(rawRequest, "reconcile request");
      const nowMs = timestamp(request.nowMs, "nowMs");
      if (state === "blocked") {
        return refusal("controller_blocked", statusReason || "controller is blocked");
      }
      if (!current || state === "idle") {
        return refusal("controller_inactive", statusReason || "controller is idle");
      }
      if (nowMs < current.lease.expires_at_ms) {
        return deepFreeze({
          accepted: true,
          changed: false,
          code: "controller_lease_current",
          status: publicStatus(),
        });
      }
      const ended = deactivate("expired");
      if (ended.error) {
        return refusal(ended.error.code, ended.error.detail || ended.error.message);
      }
      return deepFreeze({
        accepted: true,
        changed: true,
        code: "controller_lease_expired",
        status: ended.status,
      });
    }

    function block(code, error) {
      const detail = error && error.message ? error.message : String(error || code);
      const previous = current;
      const releaseError = releaseApplicationIntent(previous, code);
      forgetCurrent();
      if (releaseError) {
        const combined = `${code}: ${detail}; ${releaseError.message}`;
        transition("blocked", combined);
        return refusal(releaseError.code, combined);
      }
      transition("blocked", `${code}: ${detail}`);
      return refusal(code, detail);
    }

    function requestRevoke(rawRequest) {
      const request = objectValue(rawRequest, "revoke request");
      const match = matchingLease(request);
      if (!match.accepted) return match;
      timestamp(request.nowMs, "nowMs");
      const reason = boundedText(request.reason, "reason", 160);
      if (current.lease.takeover === "safe_point") {
        const status = transition("stopping", reason);
        return deepFreeze({
          accepted: true,
          code: "controller_safe_point_requested",
          status: status,
        });
      }
      const ended = deactivate(reason);
      if (ended.error) {
        return refusal(ended.error.code, ended.error.detail || ended.error.message);
      }
      return deepFreeze({
        accepted: true,
        code: "controller_revoked",
        status: ended.status,
      });
    }

    function acknowledgeSafePoint(rawRequest) {
      const request = objectValue(rawRequest, "safe-point acknowledgement");
      const match = matchingLease(request);
      if (!match.accepted) return match;
      timestamp(request.nowMs, "nowMs");
      if (state !== "stopping") {
        return refusal("controller_not_stopping", "no safe-point revoke is pending");
      }
      const ended = deactivate("safe_point_reached");
      if (ended.error) {
        return refusal(ended.error.code, ended.error.detail || ended.error.message);
      }
      return deepFreeze({
        accepted: true,
        code: "controller_safe_point_reached",
        status: ended.status,
      });
    }

    function matchingLease(request) {
      if (!current) {
        return refusal("stale_controller_lease", "no matching active controller lease");
      }
      const leaseId = boundedText(request.leaseId, "leaseId", 120);
      const generation = nonNegativeInteger(request.generation, "generation");
      if (
        leaseId !== current.lease.lease_id
        || generation !== current.lease.generation
      ) {
        return refusal("stale_controller_lease", "controller lease does not match");
      }
      return {accepted: true};
    }

    return Object.freeze({
      canActivate: canActivate,
      activate: activate,
      reconcile: reconcile,
      step: step,
      requestRevoke: requestRevoke,
      acknowledgeSafePoint: acknowledgeSafePoint,
      status: publicStatus,
    });
  }

  function normalizeLease(rawLease) {
    const source = objectValue(rawLease, "lease");
    const principal = String(source.principal || "").trim().toLowerCase();
    const executor = String(source.executor || "").trim().toLowerCase();
    if (principal !== PRINCIPAL) {
      throw new ControllerContractError("invalid_controller_principal", principal);
    }
    if (executor !== EXECUTOR) {
      throw new ControllerContractError("invalid_controller_executor", executor);
    }
    const issuedAtMs = timestamp(source.issued_at_ms, "issued_at_ms");
    const expiresAtMs = timestamp(source.expires_at_ms, "expires_at_ms");
    if (expiresAtMs <= issuedAtMs) {
      throw new ControllerContractError("invalid_controller_expiry");
    }
    const rate = Number(source.max_action_rate_hz);
    if (!Number.isFinite(rate) || rate <= 0 || rate > 240) {
      throw new ControllerContractError("invalid_controller_rate");
    }
    const takeover = String(source.takeover || "").trim().toLowerCase();
    if (!TAKEOVER.has(takeover)) {
      throw new ControllerContractError("invalid_controller_takeover", takeover);
    }
    return deepFreeze({
      lease_id: boundedText(source.lease_id, "lease_id", 120),
      principal: PRINCIPAL,
      executor: EXECUTOR,
      generation: nonNegativeInteger(source.generation, "generation"),
      policy_revision: nonNegativeInteger(
        source.policy_revision,
        "policy_revision"
      ),
      issued_at_ms: issuedAtMs,
      expires_at_ms: expiresAtMs,
      max_action_rate_hz: rate,
      takeover: takeover,
    });
  }

  function refusal(code, reason) {
    return deepFreeze({
      accepted: false,
      code: String(code || "controller_rejected"),
      reason: boundedOptionalText(reason, "reason", 240),
    });
  }

  function requiredSyncFunction(value, name) {
    if (typeof value !== "function") {
      throw new ControllerContractError("controller_callback_required", name);
    }
    if (isAsyncFunction(value)) {
      throw new ControllerContractError(`async_${name}_not_allowed`);
    }
    return value;
  }

  function objectValue(value, name) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new ControllerContractError("controller_object_required", name);
    }
    return value;
  }

  function frozenObject(value, name) {
    const clone = frozenJson(value, name);
    if (!clone || typeof clone !== "object" || Array.isArray(clone)) {
      throw new ControllerContractError("controller_object_required", name);
    }
    return clone;
  }

  function frozenJson(value, name) {
    try {
      const encoded = JSON.stringify(value, function (_key, item) {
        if (
          item === undefined
          || typeof item === "function"
          || typeof item === "symbol"
          || typeof item === "bigint"
          || (typeof item === "number" && !Number.isFinite(item))
        ) {
          throw new TypeError("non-json value");
        }
        return item;
      });
      if (encoded === undefined) throw new TypeError("missing JSON value");
      return deepFreeze(JSON.parse(encoded));
    } catch (error) {
      throw new ControllerContractError(
        "controller_value_not_json",
        `${name}: ${error && error.message ? error.message : String(error)}`
      );
    }
  }

  function timestamp(value, name) {
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed < 0) {
      throw new ControllerContractError("invalid_controller_timestamp", name);
    }
    return parsed;
  }

  function nonNegativeInteger(value, name) {
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed < 0) {
      throw new ControllerContractError("invalid_controller_integer", name);
    }
    return parsed;
  }

  function boundedText(value, name, limit) {
    const text = String(value === null || value === undefined ? "" : value)
      .replace(/\s+/g, " ")
      .trim();
    if (!text) throw new ControllerContractError("controller_text_required", name);
    if (text.length > limit) {
      throw new ControllerContractError("controller_text_too_long", name);
    }
    return text;
  }

  function boundedOptionalText(value, name, limit) {
    if (value === null || value === undefined || value === "") return "";
    return boundedText(value, name, limit);
  }

  function semanticType(value, name) {
    const text = boundedText(value, name, 120).toLowerCase();
    if (!/^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$/.test(text)) {
      throw new ControllerContractError("invalid_controller_action", name);
    }
    return text;
  }

  function isPromiseLike(value) {
    return Boolean(value && typeof value.then === "function");
  }

  function isAsyncFunction(value) {
    return Boolean(
      value
      && value.constructor
      && value.constructor.name === "AsyncFunction"
    );
  }

  function deepFreeze(value) {
    if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
    Object.keys(value).forEach(function (key) { deepFreeze(value[key]); });
    return Object.freeze(value);
  }

  return Object.freeze({
    createReactiveController: createReactiveController,
    ControllerContractError: ControllerContractError,
  });
});
