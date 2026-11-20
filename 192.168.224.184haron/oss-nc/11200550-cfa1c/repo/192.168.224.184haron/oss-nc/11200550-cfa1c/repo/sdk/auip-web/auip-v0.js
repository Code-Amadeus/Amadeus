(function (root, factory) {
  const managedApi = typeof module === "object" && module.exports
    ? require("../auip-core/managed-v0.js")
    : root.AmadeusAUIPManaged;
  const controllerApi = typeof module === "object" && module.exports
    ? require("../auip-core/controller-v0.js")
    : root.AmadeusAUIPController;
  const api = factory(managedApi, controllerApi);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AmadeusAUIP = api;
})(typeof globalThis !== "undefined" ? globalThis : window, function (managedApi, controllerApi) {
  "use strict";

  const DEFAULT_TIMEOUT_MS = 10000;
  const DEFAULT_SELF_ATTACH_TIMEOUT_MS = 310000;
  const DEFAULT_SELF_ATTACH_URL = "ws://127.0.0.1:17777/auip/ws";
  const LAUNCH_SCHEMA = "amadeus.auip/launch-v0";
  const LAUNCH_FRAGMENT = "amadeus-auip=";

  function readLaunchConfig(options) {
    const config = options || {};
    const locationObject = config.location || (
      typeof globalThis !== "undefined" ? globalThis.location : null
    );
    const rawHash = String(config.hash !== undefined
      ? config.hash
      : locationObject && locationObject.hash || "");
    const fragment = rawHash.charAt(0) === "#" ? rawHash.slice(1) : rawHash;
    if (!fragment.startsWith(LAUNCH_FRAGMENT)) return null;
    const encoded = fragment.slice(LAUNCH_FRAGMENT.length);
    let descriptor;
    try {
      const base64 = encoded.replace(/-/g, "+").replace(/_/g, "/")
        + "=".repeat((4 - encoded.length % 4) % 4);
      const decoded = typeof atob === "function"
        ? atob(base64)
        : Buffer.from(base64, "base64").toString("binary");
      const bytes = Uint8Array.from(decoded, function (character) {
        return character.charCodeAt(0);
      });
      descriptor = JSON.parse(new TextDecoder().decode(bytes));
    } catch (_error) {
      throw new Error("Invalid AUIP launch descriptor");
    }
    if (!descriptor || descriptor.schema !== LAUNCH_SCHEMA) {
      throw new Error("Unsupported AUIP launch descriptor");
    }
    const webSocketUrl = String(descriptor.webSocketUrl || "").trim();
    const attachTicket = String(descriptor.attachTicket || "").trim();
    const expiresAt = Number(descriptor.expiresAt || 0);
    if (!webSocketUrl || !attachTicket || !Number.isFinite(expiresAt)) {
      throw new Error("Incomplete AUIP launch descriptor");
    }
    if (expiresAt * 1000 <= Date.now()) {
      throw new Error("AUIP attach ticket expired before application startup");
    }
    if (config.consume !== false && locationObject) {
      try {
        const cleanUrl = String(locationObject.href || "").split("#", 1)[0];
        const historyObject = config.history || (
          typeof globalThis !== "undefined" ? globalThis.history : null
        );
        if (historyObject && typeof historyObject.replaceState === "function") {
          historyObject.replaceState(null, "", cleanUrl);
        }
      } catch (_error) {
        // Consumption is best effort. The ticket remains single-use and
        // short-lived even in browsers that refuse file-URL history edits.
      }
    }
    return {
      webSocketUrl: webSocketUrl,
      attachTicket: attachTicket,
      expiresAt: expiresAt,
    };
  }

  function createWebSocketTransport(options) {
    const config = options || {};
    const url = String(config.url || "").trim();
    if (!url) throw new Error("AUIP WebSocket transport requires a URL");
    const timeoutMs = Math.max(1000, Number(config.timeoutMs || DEFAULT_TIMEOUT_MS));
    const createSocket = config.webSocketFactory || function (target) { return new WebSocket(target); };
    const socket = createSocket(url);
    const pending = new Map();
    const listeners = new Set();
    let sequence = 0;
    let closed = false;
    let readyResolve;
    let readyReject;
    const ready = new Promise(function (resolve, reject) {
      readyResolve = resolve;
      readyReject = reject;
    });

    function addListener(name, listener) {
      if (typeof socket.addEventListener === "function") socket.addEventListener(name, listener);
      else if (typeof socket.on === "function") socket.on(name, listener);
      else socket["on" + name] = listener;
    }

    function rejectPending(error) {
      pending.forEach(function (item) {
        clearTimeout(item.timer);
        item.reject(error);
      });
      pending.clear();
    }

    addListener("open", function () { readyResolve(); });
    addListener("error", function () {
      const error = new Error("AUIP WebSocket failed");
      readyReject(error);
      rejectPending(error);
    });
    addListener("close", function () {
      closed = true;
      const error = new Error("AUIP WebSocket closed");
      readyReject(error);
      rejectPending(error);
    });
    addListener("message", function (event) {
      let message;
      try {
        const raw = event && event.data !== undefined ? event.data : event;
        message = JSON.parse(String(raw));
      } catch (_error) {
        return;
      }
      if (message.type === "evt") {
        listeners.forEach(function (listener) {
          listener(String(message.method || ""), message.params || {});
        });
        return;
      }
      if (message.type !== "res") return;
      const requestId = String(message.id || "");
      const item = pending.get(requestId);
      if (!item) return;
      pending.delete(requestId);
      clearTimeout(item.timer);
      const result = message.params || {};
      if (result.ok === false || result.error) {
        const error = new Error(String(result.error || "AUIP request failed"));
        error.code = String(result.error || "");
        error.detail = String(result.detail || "");
        item.reject(error);
      } else {
        item.resolve(result);
      }
    });
    if (Number(socket.readyState) === 1) readyResolve();

    return {
      async request(method, params, requestOptions) {
        if (closed) throw new Error("AUIP WebSocket is closed");
        await ready;
        const requestId = "auip-" + Date.now().toString(36) + "-" + (++sequence).toString(36);
        const requestTimeoutMs = Math.max(
          1000,
          Number(requestOptions && requestOptions.timeoutMs || timeoutMs)
        );
        return new Promise(function (resolve, reject) {
          const timer = setTimeout(function () {
            pending.delete(requestId);
            reject(new Error("AUIP request timed out: " + method));
          }, requestTimeoutMs);
          pending.set(requestId, {resolve: resolve, reject: reject, timer: timer});
          socket.send(JSON.stringify({
            type: "req",
            id: requestId,
            method: String(method || ""),
            params: params || {},
          }));
        });
      },
      onEvent(listener) {
        listeners.add(listener);
        return function () { listeners.delete(listener); };
      },
      close() {
        if (closed) return;
        closed = true;
        if (typeof socket.close === "function") socket.close();
        rejectPending(new Error("AUIP transport closed"));
        listeners.clear();
      },
    };
  }

  function createApp(options) {
    const config = options || {};
    const manifest = config.manifest;
    const launchConfig = config.launchConfig || (
      !config.transport && !config.webSocketUrl && !config.attachTicket
        ? readLaunchConfig()
        : null
    );
    let attachTicket = String(
      config.attachTicket || launchConfig && launchConfig.attachTicket || ""
    ).trim();
    const canCreateSocket = typeof config.webSocketFactory === "function"
      || typeof WebSocket !== "undefined";
    const selfAttachUrl = config.selfAttach === false || !canCreateSocket
      ? ""
      : String(config.selfAttachUrl || DEFAULT_SELF_ATTACH_URL).trim();
    const webSocketUrl = String(
      config.webSocketUrl || launchConfig && launchConfig.webSocketUrl || selfAttachUrl
    ).trim();
    const transport = config.transport || (
      webSocketUrl
        ? createWebSocketTransport({
            url: webSocketUrl,
            timeoutMs: config.timeoutMs,
            webSocketFactory: config.webSocketFactory,
          })
        : null
    );
    const getState = typeof config.getState === "function" ? config.getState : function () { return {}; };
    const onAction = typeof config.onAction === "function" ? config.onAction : null;
    const onActionStarted = typeof config.onActionStarted === "function"
      ? config.onActionStarted
      : null;
    const onActionSettled = typeof config.onActionSettled === "function"
      ? config.onActionSettled
      : null;
    const onControllerRevoke = typeof config.onControllerRevoke === "function"
      ? config.onControllerRevoke
      : null;
    let appSessionId = "";
    let bridgeToken = "";
    let revision = 0;
    let unsubscribe = null;
    let startPromise = null;
    let inboundControlQueue = Promise.resolve();
    const instanceId = String(config.instanceId || randomId("instance"));

    function enqueueInboundControl(handler) {
      const queued = inboundControlQueue
        .catch(function () {})
        .then(handler);
      inboundControlQueue = queued;
      return queued;
    }

    function requireTransport() {
      if (!transport) throw new Error("AUIP is unavailable: no attach transport was provided");
      return transport;
    }

    function auth(params) {
      return Object.assign({app_session_id: appSessionId, bridge_token: bridgeToken}, params || {});
    }

    async function publishState(nextRevision, explicitState) {
      const state = explicitState === undefined ? await getState() : explicitState;
      const result = await requireTransport().request("auip.state.publish", auth({
        revision: Number(nextRevision),
        state: state || {},
      }));
      revision = Number(result.revision);
      return result;
    }

    async function publishEvent(type, payload, options) {
      const event = options || {};
      return requireTransport().request("auip.event.publish", auth({
        event_id: String(event.eventId || randomId("event")),
        event_type: String(type || ""),
        actor: String(event.actor || "app"),
        revision: event.revision === undefined ? revision : Number(event.revision),
        payload: payload || {},
        caused_by_action_id: String(event.causedByActionId || ""),
      }));
    }

    async function publishControllerStatus(lease, status) {
      const authority = lease && typeof lease === "object" ? lease : {};
      const projection = status && typeof status === "object" ? status : {};
      return requireTransport().request("auip.controller.status.publish", auth({
        lease_id: String(authority.lease_id || authority.leaseId || ""),
        generation: Number(authority.generation),
        status: String(projection.status || ""),
        reason: String(projection.reason || ""),
      }));
    }

    async function handleControllerRevoke(params) {
      if (!onControllerRevoke || String(params.app_session_id || "") !== appSessionId) return;
      const revoke = params.revoke && typeof params.revoke === "object"
        ? params.revoke
        : {};
      try {
        await onControllerRevoke(revoke);
      } catch (error) {
        if (typeof console !== "undefined" && console.error) {
          console.error("AUIP Controller revoke handling failed", error);
        }
      }
    }

    async function handleRequestedAction(params) {
      if (!onAction || String(params.app_session_id || "") !== appSessionId) return;
      const action = params.action || {};
      let outcome;
      let result;
      try {
        if (onActionStarted) onActionStarted(action);
        outcome = await onAction(action, await getState());
      } catch (error) {
        outcome = {accepted: false, reason: error && error.message ? error.message : String(error)};
      }
      try {
        outcome = outcome || {};
        const accepted = outcome.accepted === true;
        const resultingRevision = accepted ? Number(outcome.revision) : Number(action.expected_revision);
        const hasExplicitState = Object.prototype.hasOwnProperty.call(outcome, "state");
        result = await requireTransport().request("auip.action.result", auth({
          action_id: String(action.action_id || ""),
          accepted: accepted,
          resulting_revision: resultingRevision,
          // Legacy createApp callers retain the fallback. createManagedApp
          // always supplies an atomic state and never takes this branch.
          state: accepted ? (hasExplicitState ? outcome.state : await getState()) : undefined,
          effects: outcome.effects || {},
          reason: String(outcome.reason || ""),
        }));
        const receiptRevision = Number(result.revision);
        revision = receiptRevision;
        if (accepted && Array.isArray(outcome.events)) {
          const eventErrors = [];
          for (const event of outcome.events.slice(0, 4)) {
            if (!event || typeof event !== "object") continue;
            try {
              await publishEvent(String(event.type || ""), event.payload || {}, {
                eventId: String(event.eventId || randomId("event")),
                actor: String(event.actor || "app"),
                revision: receiptRevision,
                causedByActionId: String(action.action_id || ""),
              });
            } catch (error) {
              eventErrors.push(error);
            }
          }
          if (eventErrors.length && typeof console !== "undefined" && console.error) {
            console.error("AUIP action committed, but one or more semantic events were rejected", eventErrors[0]);
          }
        }
      } finally {
        if (onActionSettled) {
          try {
            await onActionSettled({action: action, outcome: outcome, result: result});
          } catch (error) {
            // Lifecycle observation cannot rewrite an action receipt, but a
            // Controller activation failure must remain visible.
            if (typeof console !== "undefined" && console.error) {
              console.error("AUIP post-receipt lifecycle failed", error);
            }
          }
        }
      }
    }

    async function startConnected() {
        if (appSessionId) return app.session();
        const activeTransport = requireTransport();
        if (!attachTicket) {
          const locationObject = config.location || (
            typeof globalThis !== "undefined" ? globalThis.location : null
          );
          const entryUrl = String(
            config.entryUrl || locationObject && locationObject.href || ""
          ).split("#", 1)[0];
          const approved = await activeTransport.request(
            "auip.attach.request",
            {
              manifest: manifest,
              instance_id: instanceId,
              entry_url: entryUrl,
            },
            {
              timeoutMs: Math.max(
                DEFAULT_SELF_ATTACH_TIMEOUT_MS,
                Number(config.selfAttachTimeoutMs || 0)
              ),
            }
          );
          attachTicket = String(approved.attach_ticket || "").trim();
          if (!attachTicket) throw new Error("AUIP attach request returned no ticket");
        }
        const registered = await activeTransport.request("auip.register", {
          manifest: manifest,
          attach_ticket: attachTicket,
        });
        appSessionId = String(registered.app_session_id || "");
        bridgeToken = String(registered.bridge_token || "");
        revision = Number(registered.revision || 0);
        unsubscribe = activeTransport.onEvent(function (method, params) {
          if (method === "auip.action.requested") {
            void enqueueInboundControl(function () {
              return handleRequestedAction(params);
            });
          }
          if (method === "auip.controller.revoke.requested") {
            void enqueueInboundControl(function () {
              return handleControllerRevoke(params);
            });
          }
        });
        return app.session();
    }

    const app = {
      async start() {
        if (appSessionId) return this.session();
        if (!startPromise) {
          startPromise = startConnected().catch(function (error) {
            startPromise = null;
            throw error;
          });
        }
        return startPromise;
      },
      publishState: publishState,
      emit: publishEvent,
      publishControllerStatus: publishControllerStatus,
      session() {
        return {appSessionId: appSessionId, revision: revision, active: Boolean(appSessionId)};
      },
      async settled() {
        return inboundControlQueue;
      },
      async close(reason) {
        await inboundControlQueue.catch(function () {});
        if (!appSessionId) return {ok: true};
        const result = await requireTransport().request(
          "auip.session.close",
          auth({reason: String(reason || "")})
        );
        if (unsubscribe) unsubscribe();
        unsubscribe = null;
        appSessionId = "";
        bridgeToken = "";
        attachTicket = "";
        startPromise = null;
        return result;
      },
      dispose() {
        if (unsubscribe) unsubscribe();
        unsubscribe = null;
        if (transport && typeof transport.close === "function") transport.close();
      },
    };
    return app;
  }

  function createManagedApp(options) {
    if (!managedApi || typeof managedApi.createManagedCore !== "function") {
      throw new Error("AUIP managed core is unavailable; load auip-core/managed-v0.js before auip-v0.js");
    }
    const config = options || {};
    const diagnostic = typeof config.onDiagnostic === "function"
      ? config.onDiagnostic
      : function () {};
    const manifest = config.manifest && typeof config.manifest === "object"
      ? config.manifest
      : {};
    const controllerProfile = manifest.controller && typeof manifest.controller === "object"
      ? manifest.controller
      : null;
    const controllerPolicyActions = new Set(
      controllerProfile && Array.isArray(controllerProfile.policyActions)
        ? controllerProfile.policyActions.map(function (value) {
            return String(value || "").trim();
          }).filter(Boolean)
        : []
    );
    const controllerConfig = config.controller && typeof config.controller === "object"
      ? config.controller
      : null;
    if (controllerProfile && (!controllerApi || typeof controllerApi.createReactiveController !== "function")) {
      throw new Error("AUIP Controller Core is unavailable; load controller-v0.js before auip-v0.js");
    }
    if (controllerProfile && !controllerConfig) {
      throw new Error("AUIP Controller profile requires application callbacks");
    }
    if (controllerProfile && typeof controllerConfig.policySummary !== "function") {
      throw new Error("AUIP Controller profile requires policySummary(action payload)");
    }
    const controller = controllerProfile
      ? controllerApi.createReactiveController(controllerConfig)
      : null;
    const readApplicationSnapshot = typeof config.snapshot === "function"
      ? config.snapshot
      : config.getState;
    if (typeof readApplicationSnapshot !== "function") {
      throw new Error("AUIP managed app requires snapshot");
    }
    if (controllerProfile) {
      const policyAction = Array.isArray(controllerProfile.policyActions)
        ? String(controllerProfile.policyActions[0] || "")
        : "";
      const probe = Object.freeze({
        kind: "controller/v1",
        status: "active",
        policyRevision: 0,
        policyAction: policyAction,
        policySummary: "AUIP controller projection probe",
      });
      const projected = findSituation(
        readApplicationSnapshot({controller: probe}),
        "controller/v1"
      );
      if (
        !projected
        || projected.status !== probe.status
        || Number(projected.policyRevision) !== probe.policyRevision
        || projected.policyAction !== probe.policyAction
        || projected.policySummary !== probe.policySummary
      ) {
        throw new Error(
          "AUIP Controller snapshot must preserve the supplied governance status"
        );
      }
    }
    const core = managedApi.createManagedCore({
      manifest: manifest,
      snapshot: function (context) {
        return readApplicationSnapshot(Object.assign({}, context || {}, {
          controller: controller ? controller.status() : null,
        }));
      },
      actions: config.actions || {},
      initialRevision: config.initialRevision,
      maxStateBytes: config.maxStateBytes,
      projectionBudgetChars: config.projectionBudgetChars,
      onDiagnostic: diagnostic,
    });
    let connected = false;
    let publicationQueue = Promise.resolve();
    let actionBarrier = null;
    let releaseActionBarrier = null;
    let managedStartPromise = null;
    let activeControllerLease = null;
    let lastSharedBoundaryAtMs = null;
    let lastBackgroundCheckpointProbeAtMs = null;

    function markSharedBoundary(envelope) {
      if (envelope && envelope.committed === true) {
        lastSharedBoundaryAtMs = Date.now();
        lastBackgroundCheckpointProbeAtMs = lastSharedBoundaryAtMs;
      }
      return envelope;
    }

    function beginHostAction() {
      if (actionBarrier) return;
      actionBarrier = new Promise(function (resolve) {
        releaseActionBarrier = resolve;
      });
    }

    function settleHostAction() {
      if (releaseActionBarrier) releaseActionBarrier();
      actionBarrier = null;
      releaseActionBarrier = null;
    }

    async function publishControllerTransition(lease) {
      const checkpoint = markSharedBoundary(
        core.checkpoint({actor: "app", events: []})
      );
      await enqueue(checkpoint);
      await base.publishControllerStatus(lease, controller.status());
      return checkpoint;
    }

    function observeControllerTransition(publication, lease) {
      // A local expiry and a newer Host lease may cross in flight. The Host
      // must still reject the superseded status publication, but continuous
      // app loops are not required to await every transition promise merely
      // to prevent an unhandled rejection. Keep the original rejecting promise
      // available to explicit callers and record the failure once here.
      publication.catch(function (error) {
        diagnostic({
          code: "controller_status_publication_failed",
          leaseId: String(lease && lease.lease_id || ""),
          generation: Number(lease && lease.generation || 0),
          reason: error && error.message ? error.message : String(error),
        });
      });
      return publication;
    }

    function reconcileControllerAtBoundary() {
      if (!controller || !activeControllerLease) {
        return {changed: false, result: null};
      }
      const lease = activeControllerLease;
      const before = JSON.stringify(controller.status());
      const result = controller.reconcile({nowMs: Date.now()});
      const status = controller.status();
      if (JSON.stringify(status) === before) {
        return {changed: false, result: result};
      }
      if (status.status === "idle") activeControllerLease = null;
      // The following action/checkpoint publishes the reconciled snapshot at
      // its own revision. Report only governance here, avoiding an extra
      // checkpoint that would make the incoming ordinary action stale.
      observeControllerTransition(
        base.publishControllerStatus(lease, status),
        lease
      );
      return {changed: true, lease: lease, result: result};
    }

    async function settleControllerPolicy(info) {
      settleHostAction();
      if (!controller) return;
      const action = info && info.action && typeof info.action === "object"
        ? info.action
        : {};
      const result = info && info.result && typeof info.result === "object"
        ? info.result
        : {};
      const receipt = result.receipt && typeof result.receipt === "object"
        ? result.receipt
        : {};
      const lease = action.controller_lease;
      if (!lease || receipt.accepted !== true) return;
      let summary;
      try {
        summary = controllerConfig.policySummary({
          actionType: String(action.type || ""),
          policy: action.payload || {},
          effects: receipt.effects || {},
        });
      } catch (error) {
        diagnostic({
          code: "controller_policy_summary_failed",
          actionType: String(action.type || ""),
          reason: error && error.message ? error.message : String(error),
        });
        summary = String(action.type || "controller policy");
      }
      if (summary && typeof summary.then === "function") {
        throw new Error("AUIP Controller policySummary must be synchronous");
      }
      if (!String(summary || "").trim()) {
        diagnostic({
          code: "controller_policy_summary_empty",
          actionType: String(action.type || ""),
        });
        summary = String(action.type || "controller policy");
      }
      const activation = controller.activate({
        lease: lease,
        actionType: String(action.type || ""),
        policy: action.payload || {},
        policySummary: String(summary || ""),
      });
      if (!activation || activation.accepted !== true) {
        const code = String(
          activation && activation.code || "controller_activation_rejected"
        );
        const reason = String(activation && activation.reason || "");
        diagnostic({
          code: code,
          actionType: String(action.type || ""),
          reason: reason,
          generation: Number(lease.generation),
        });
        if (controller.status().status !== "active") {
          activeControllerLease = null;
          await publishControllerTransition(lease);
        }
        throw new Error(reason ? `${code}: ${reason}` : code);
      }
      activeControllerLease = lease;
      await publishControllerTransition(lease);
    }

    function dispatchManagedAction(action) {
      const request = action && typeof action === "object" ? action : {};
      const revisionBeforeReconcile = core.revision();
      const reconciliation = reconcileControllerAtBoundary();
      const actionType = String(request.type || "").trim();
      const lease = request.controller_lease;
      const expectedRevision = Number(request.expected_revision);
      let currentRevision = core.revision();
      let effectiveRequest = request;
      let expiryCheckpoint = null;
      let boundaryRebased = false;
      if (reconciliation.changed === true) {
        expiryCheckpoint = markSharedBoundary(core.checkpointIfChanged({
          actor: "app",
          events: [],
        }));
        currentRevision = core.revision();
        if (
          expiryCheckpoint.committed === true
          && Number.isInteger(expectedRevision)
          && expectedRevision === revisionBeforeReconcile
        ) {
          effectiveRequest = Object.assign({}, request, {
            expected_revision: currentRevision,
          });
          boundaryRebased = true;
          diagnostic({
            code: "controller_expiry_action_boundary_rebased",
            actionType: actionType,
            fromRevision: expectedRevision,
            toRevision: currentRevision,
          });
        }
      }
      const isControllerPolicyAction = Boolean(
        controller && controllerPolicyActions.has(actionType)
      );
      if (
        expiryCheckpoint
        && expiryCheckpoint.committed === true
        && !boundaryRebased
        && !(
          isControllerPolicyAction
          && Number.isInteger(expectedRevision)
          && expectedRevision <= currentRevision
        )
      ) {
        enqueue(expiryCheckpoint);
      }
      if (isControllerPolicyAction) {
        if (request.actor !== "kurisu") {
          return {
            accepted: false,
            code: "invalid_controller_policy_actor",
            reason: "invalid_controller_policy_actor: Controller policy actions require Host participant authority",
          };
        }
        if (!lease || typeof lease !== "object") {
          return {
            accepted: false,
            code: "controller_lease_required",
            reason: "controller_lease_required: Controller policy action requires a Host lease",
          };
        }
        const admission = controller.canActivate(lease);
        if (!admission || admission.accepted !== true) {
          const code = String(
            admission && admission.code || "controller_lease_rejected"
          );
          const reason = String(admission && admission.reason || "");
          diagnostic({
            code: code,
            actionType: actionType,
            reason: reason,
            generation: Number(lease.generation),
          });
          return {
            accepted: false,
            code: code,
            reason: reason ? `${code}: ${reason}` : code,
          };
        }
      }
      const mayRebindControllerPolicy = Boolean(
        isControllerPolicyAction
        && Number.isInteger(expectedRevision)
        && expectedRevision <= currentRevision
      );
      if (!mayRebindControllerPolicy) {
        return markSharedBoundary(core.dispatchAction(effectiveRequest));
      }

      // The Host already rebases an approved Controller policy to its latest
      // telemetry revision. A continuous app may checkpoint once more while
      // that authenticated request is in flight. Close only that transport
      // race at the app's atomic dispatch boundary; ordinary Decision actions
      // retain exact revision matching and the app handler still owns current
      // policy preconditions.
      const telemetryCheckpoint = core.checkpointIfChanged({
        actor: "app",
        events: [],
      });
      if (telemetryCheckpoint.committed === true) {
        currentRevision = core.revision();
        diagnostic({
          code: "controller_policy_private_telemetry_checkpoint",
          actionType: actionType,
          revision: currentRevision,
          generation: Number(lease.generation),
        });
      }
      if (expectedRevision !== currentRevision) {
        diagnostic({
          code: "controller_policy_locally_rebased",
          actionType: actionType,
          fromRevision: expectedRevision,
          toRevision: currentRevision,
          generation: Number(lease.generation),
        });
      }
      return markSharedBoundary(
        core.dispatchAction(Object.assign({}, request, {
          expected_revision: currentRevision,
        }))
      );
    }

    async function revokeController(revoke) {
      if (!controller) return;
      const request = revoke && typeof revoke === "object" ? revoke : {};
      const outcome = controller.requestRevoke({
        leaseId: String(request.lease_id || ""),
        generation: Number(request.generation),
        nowMs: Number(request.requested_at_ms),
        reason: String(request.reason || "controller_revoked"),
      });
      if (outcome.accepted !== true) {
        diagnostic({
          code: String(outcome.code || "controller_revoke_rejected"),
          reason: String(outcome.reason || ""),
        });
        return;
      }
      await publishControllerTransition(request);
      if (controller.status().status === "idle") activeControllerLease = null;
    }

    const base = createApp(Object.assign({}, config, {
      manifest: core.manifest,
      getState: function () { return core.snapshot(); },
      onActionStarted: beginHostAction,
      onAction: function (action) {
        const outcome = dispatchManagedAction(action);
        if (!outcome.accepted) {
          return {
            accepted: false,
            reason: outcome.reason,
          };
        }
        return {
          accepted: true,
          revision: outcome.revision,
          state: outcome.state,
          effects: outcome.effects,
          events: outcome.events,
        };
      },
      onActionSettled: settleControllerPolicy,
      onControllerRevoke: revokeController,
    }));

    async function publishEnvelope(envelope) {
      await base.publishState(envelope.revision, envelope.state);
      const failures = [];
      for (const event of envelope.events) {
        try {
          await base.emit(event.type, event.payload, {
            eventId: event.eventId,
            actor: event.actor,
            revision: envelope.revision,
          });
        } catch (error) {
          failures.push(error);
        }
      }
      if (failures.length) {
        diagnostic({
          code: "semantic_event_publication_failed",
          revision: envelope.revision,
          failures: failures.length,
          reason: failures[0] && failures[0].message
            ? failures[0].message
            : String(failures[0]),
        });
      }
      return {status: failures.length ? "state_published_with_event_errors" : "published"};
    }

    function enqueue(envelope) {
      if (!envelope || envelope.committed !== true) {
        return Promise.resolve({status: "not_committed"});
      }
      const barrier = actionBarrier;
      const publication = publicationQueue
        .catch(function () {})
        .then(async function () {
          if (barrier) await barrier;
          if (!connected) return {status: "standalone"};
          return publishEnvelope(envelope);
        });
      publicationQueue = publication;
      publication.catch(function (error) {
        diagnostic({
          code: "state_publication_failed",
          revision: envelope.revision,
          reason: error && error.message ? error.message : String(error),
        });
      });
      return publication;
    }

    function withPublication(envelope) {
      markSharedBoundary(envelope);
      return Object.freeze(Object.assign({}, envelope, {
        publication: enqueue(envelope),
      }));
    }

    function checkpointIfDue(specification) {
      const reconciliation = reconcileControllerAtBoundary();
      const requested = specification && typeof specification === "object"
        ? specification
        : {};
      const minimumIntervalMs = requested.minimumIntervalMs === undefined
        ? 2000
        : Number(requested.minimumIntervalMs);
      if (!Number.isInteger(minimumIntervalMs) || minimumIntervalMs <= 0) {
        throw new Error("checkpointIfDue minimumIntervalMs must be a positive integer");
      }
      if (
        requested.events !== undefined
        && (!Array.isArray(requested.events) || requested.events.length > 0)
      ) {
        throw new Error("checkpointIfDue cannot carry semantic events; use checkpoint");
      }
      if (
        requested.effects !== undefined
        && (
          !requested.effects
          || typeof requested.effects !== "object"
          || Array.isArray(requested.effects)
          || Object.keys(requested.effects).length > 0
        )
      ) {
        throw new Error("checkpointIfDue cannot carry semantic effects; use checkpoint");
      }
      const nowMs = Date.now();
      if (
        reconciliation.changed !== true
        && lastBackgroundCheckpointProbeAtMs !== null
        && nowMs - lastBackgroundCheckpointProbeAtMs < minimumIntervalMs
      ) {
        return Object.freeze({
          committed: false,
          revision: core.revision(),
          code: "checkpoint_not_due",
          publication: Promise.resolve({status: "not_due"}),
        });
      }
      const checkpoint = core.checkpointIfChanged({
        actor: requested.actor || "app",
        events: [],
      });
      if (checkpoint.committed !== true) {
        lastBackgroundCheckpointProbeAtMs = nowMs;
        return Object.freeze(Object.assign({}, checkpoint, {
          publication: Promise.resolve({status: "unchanged"}),
        }));
      }
      return withPublication(checkpoint);
    }

    const managed = {
      async start() {
        if (connected) return this.session();
        if (!managedStartPromise) {
          managedStartPromise = (async function () {
            await base.start();
            connected = true;
            if (typeof config.onConnected === "function") {
              const callbackResult = config.onConnected();
              if (callbackResult && typeof callbackResult.then === "function") {
                throw new Error("createManagedApp onConnected must be synchronous");
              }
            }
            const initial = markSharedBoundary(core.checkpoint({
              actor: "app",
              events: config.initialEvents,
            }));
            await enqueue(initial);
            return managed.session();
          })().catch(async function (error) {
            connected = false;
            managedStartPromise = null;
            // Registration alone is not a usable AppSession. If the initial
            // Host-validated snapshot fails, close the bound session instead
            // of leaving a revision-zero surface that can receive actions
            // while the local Managed Core has already advanced.
            try {
              await base.close("initial_state_publish_failed");
            } catch (closeError) {
              diagnostic({
                code: "initialization_close_failed",
                reason: closeError && closeError.message
                  ? closeError.message
                  : String(closeError),
              });
            }
            throw error;
          });
        }
        return managedStartPromise;
      },
      commitLocal(specification) {
        const reconciliation = reconcileControllerAtBoundary();
        if (reconciliation.changed === true) {
          const checkpoint = markSharedBoundary(core.checkpointIfChanged({
            actor: "app",
            events: [],
          }));
          enqueue(checkpoint);
        }
        return withPublication(core.commitLocal(specification));
      },
      checkpoint(specification) {
        reconcileControllerAtBoundary();
        return withPublication(core.checkpoint(specification));
      },
      checkpointIfDue: checkpointIfDue,
      revision() { return core.revision(); },
      snapshot() { return core.snapshot(); },
      controllerStatus() {
        if (!controller) return null;
        return controller.status();
      },
      controllerStep(specification) {
        if (!controller) throw new Error("AUIP Controller is not configured");
        const requested = specification && typeof specification === "object"
          ? specification
          : {};
        // Host leases use Unix epoch milliseconds. Animation callbacks often
        // supply performance.now(), whose origin is page start and therefore
        // can never reach an epoch expiry. The Web binding owns this clock
        // boundary; the transport-neutral Core remains explicitly clocked for
        // deterministic tests and non-Web bindings.
        const step = Object.assign({}, requested, {nowMs: Date.now()});
        // Continuous applications call this from their own loop before,
        // during, and after a Host lease. The Controller Core already owns
        // the idle refusal contract; do not turn ordinary idle time into an
        // exception that can terminate the application's render/update loop.
        if (!activeControllerLease) return controller.step(step);
        const before = JSON.stringify(controller.status());
        const result = controller.step(step);
        const changed = JSON.stringify(controller.status()) !== before;
        if (!changed) return result;
        const lease = activeControllerLease;
        const publication = observeControllerTransition(
          publishControllerTransition(lease),
          lease
        );
        if (controller.status().status === "idle") activeControllerLease = null;
        return Object.freeze(Object.assign({}, result, {publication: publication}));
      },
      acknowledgeControllerSafePoint(specification) {
        if (!controller || !activeControllerLease) {
          throw new Error("AUIP Controller is not active");
        }
        const lease = activeControllerLease;
        const result = controller.acknowledgeSafePoint({
          leaseId: String(lease.lease_id || ""),
          generation: Number(lease.generation),
          nowMs: Date.now(),
        });
        const publication = observeControllerTransition(
          publishControllerTransition(lease),
          lease
        );
        if (controller.status().status === "idle") activeControllerLease = null;
        return Object.freeze(Object.assign({}, result, {publication: publication}));
      },
      session() {
        return Object.assign({}, base.session(), {
          revision: core.revision(),
          connected: connected,
        });
      },
      async settled() {
        await base.settled();
        return publicationQueue;
      },
      async close(reason) {
        try {
          await publicationQueue;
        } catch (_error) {
          // Closing the Host projection must remain possible after a failed publish.
        }
        const result = await base.close(reason);
        connected = false;
        managedStartPromise = null;
        settleHostAction();
        return result;
      },
      dispose() {
        connected = false;
        settleHostAction();
        base.dispose();
      },
      healthy() { return core.healthy(); },
    };
    return Object.freeze(managed);
  }

  function findSituation(value, kind, seen) {
    if (!value || typeof value !== "object") return null;
    const visited = seen || new WeakSet();
    if (visited.has(value)) return null;
    visited.add(value);
    if (!Array.isArray(value) && String(value.kind || "") === kind) return value;
    const children = Array.isArray(value) ? value : Object.keys(value).map(function (key) {
      return value[key];
    });
    for (const child of children) {
      const found = findSituation(child, kind, visited);
      if (found) return found;
    }
    return null;
  }

  function randomId(prefix) {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return prefix + "_" + crypto.randomUUID();
    }
    return prefix + "_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2);
  }

  return {
    createApp: createApp,
    createManagedApp: createManagedApp,
    createWebSocketTransport: createWebSocketTransport,
    readLaunchConfig: readLaunchConfig,
  };
});
