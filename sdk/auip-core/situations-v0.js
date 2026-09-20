(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AmadeusAUIPSituations = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  class SituationProjectionError extends Error {
    constructor(code, detail) {
      const cleanCode = String(code || "invalid_situation_projection");
      const cleanDetail = String(detail || "");
      super(cleanDetail ? `${cleanCode}: ${cleanDetail}` : cleanCode);
      this.name = "SituationProjectionError";
      this.code = cleanCode;
      this.detail = cleanDetail;
    }
  }

  function gridSituation(options) {
    const config = options && typeof options === "object" ? options : {};
    const width = positiveInteger(config.width, "width");
    const height = positiveInteger(config.height, "height");
    const empty = oneSymbol(config.empty === undefined ? "." : config.empty, "empty");
    if (typeof config.cell !== "function") {
      throw new SituationProjectionError("grid_cell_reader_required");
    }
    const legend = normalizeLegend(config.legend, empty);
    const allowed = new Set([empty, ...Object.keys(legend)]);
    const rows = [];
    for (let y = 0; y < height; y += 1) {
      const symbols = [];
      for (let x = 0; x < width; x += 1) {
        const symbol = oneSymbol(config.cell(x, y), `cell[${x},${y}]`);
        if (!allowed.has(symbol)) {
          throw new SituationProjectionError(
            "grid_symbol_not_in_legend",
            `${symbol} at ${x},${y}`
          );
        }
        symbols.push(symbol);
      }
      rows.push(symbols.join(""));
    }
    return deepFreeze({
      kind: "grid/v1",
      width: width,
      height: height,
      empty: empty,
      legend: legend,
      rows: rows,
    });
  }

  function choiceSituation(options) {
    const config = options && typeof options === "object" ? options : {};
    const source = Array.isArray(config.options) ? config.options : null;
    if (!source || source.length === 0) {
      throw new SituationProjectionError("choice_options_required");
    }
    const compact = config.compact === true;
    const actionAddressed = config.actionAddressed === true;
    if (compact && actionAddressed) {
      throw new SituationProjectionError("choice_projection_modes_conflict");
    }
    const defaultAction = config.action === undefined
      ? ""
      : semanticType(config.action, "action");
    let declaredActionTypes = null;
    if (config.actionTypes !== undefined) {
      if (!Array.isArray(config.actionTypes) || config.actionTypes.length === 0) {
        throw new SituationProjectionError("choice_action_types_required");
      }
      if (config.actionTypes.length > 64) {
        throw new SituationProjectionError("choice_action_types_too_many");
      }
      const seenActionTypes = new Set();
      declaredActionTypes = config.actionTypes.map(function (value, index) {
        const actionType = semanticType(value, `actionTypes[${index}]`);
        if (seenActionTypes.has(actionType)) {
          throw new SituationProjectionError("choice_action_type_duplicate", actionType);
        }
        seenActionTypes.add(actionType);
        return actionType;
      });
    }
    if (compact && !defaultAction) {
      throw new SituationProjectionError("choice_compact_action_required");
    }
    const ids = new Set();
    const normalized = source.map(function (raw, index) {
      const item = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
      const id = boundedText(item.id, `options[${index}].id`, 80);
      if (ids.has(id)) {
        throw new SituationProjectionError("choice_option_duplicate", id);
      }
      ids.add(id);
      const action = semanticType(
        item.action === undefined ? defaultAction : item.action,
        `options[${index}].action`
      );
      const available = item.available;
      if (typeof available !== "boolean") {
        throw new SituationProjectionError("choice_availability_required", id);
      }
      const payload = jsonObjectClone(item.payload, `options[${index}].payload`);
      return {
        id: id,
        label: boundedText(item.label, `options[${index}].label`, 120),
        action: action,
        payload: payload,
        available: available,
      };
    });
    const derivedActionTypes = Array.from(new Set(normalized.map(function (item) {
      return item.action;
    })));
    const governedActionTypes = declaredActionTypes || derivedActionTypes;
    normalized.forEach(function (item) {
      if (!governedActionTypes.includes(item.action)) {
        throw new SituationProjectionError(
          "choice_option_action_not_governed",
          item.action
        );
      }
    });
    if (compact) {
      normalized.forEach(function (item) {
        if (item.action !== defaultAction) {
          throw new SituationProjectionError(
            "choice_compact_action_mismatch",
            item.id
          );
        }
        if (item.available !== true) {
          throw new SituationProjectionError(
            "choice_compact_unavailable_option",
            item.id
          );
        }
      });
      if (
        governedActionTypes.length !== 1
        || governedActionTypes[0] !== defaultAction
      ) {
        throw new SituationProjectionError("choice_compact_action_types_mismatch");
      }
      const compactResult = {
        kind: "choice/v1",
        action: defaultAction,
        options: normalized.map(function (item) {
          return {
            label: item.label,
            payload: item.payload,
          };
        }),
      };
      if (declaredActionTypes) {
        compactResult.actionTypes = governedActionTypes;
      }
      return deepFreeze(compactResult);
    }
    if (actionAddressed) {
      const exactOptions = new Set();
      const projected = normalized.map(function (item) {
        const identity = item.action + "\u0000" + JSON.stringify(item.payload);
        if (exactOptions.has(identity)) {
          throw new SituationProjectionError(
            "choice_action_payload_duplicate",
            item.action
          );
        }
        exactOptions.add(identity);
        return {
          action: item.action,
          payload: item.payload,
          available: item.available,
        };
      });
      const actionAddressedResult = {
        kind: "choice/v1",
        options: projected,
      };
      if (declaredActionTypes) {
        actionAddressedResult.actionTypes = governedActionTypes;
      }
      return deepFreeze(actionAddressedResult);
    }
    const result = {
      kind: "choice/v1",
      options: normalized,
    };
    if (declaredActionTypes) {
      result.actionTypes = governedActionTypes;
    }
    return deepFreeze(result);
  }

  function actionAvailabilitySituation(options) {
    const config = options && typeof options === "object" ? options : {};
    if (!Array.isArray(config.actionTypes) || config.actionTypes.length === 0) {
      throw new SituationProjectionError("action_availability_types_required");
    }
    if (config.actionTypes.length > 128) {
      throw new SituationProjectionError("action_availability_types_too_many");
    }
    if (!Array.isArray(config.availableActionTypes)) {
      throw new SituationProjectionError("available_action_types_required");
    }
    if (config.availableActionTypes.length > 128) {
      throw new SituationProjectionError("available_action_types_too_many");
    }
    const familySeen = new Set();
    const actionTypes = config.actionTypes.map(function (value, index) {
      const actionType = semanticType(value, `actionTypes[${index}]`);
      if (familySeen.has(actionType)) {
        throw new SituationProjectionError(
          "action_availability_type_duplicate",
          actionType
        );
      }
      familySeen.add(actionType);
      return actionType;
    });
    const availableSeen = new Set();
    const availableActionTypes = config.availableActionTypes.map(function (value, index) {
      const actionType = semanticType(value, `availableActionTypes[${index}]`);
      if (!familySeen.has(actionType)) {
        throw new SituationProjectionError(
          "available_action_type_not_governed",
          actionType
        );
      }
      if (availableSeen.has(actionType)) {
        throw new SituationProjectionError(
          "available_action_type_duplicate",
          actionType
        );
      }
      availableSeen.add(actionType);
      return actionType;
    });
    return deepFreeze({
      kind: "action_availability/v1",
      actionTypes: actionTypes,
      availableActionTypes: availableActionTypes,
    });
  }

  function scalarSituation(options) {
    const config = options && typeof options === "object" ? options : {};
    const source = Array.isArray(config.metrics) ? config.metrics : null;
    if (!source || source.length === 0) {
      throw new SituationProjectionError("scalar_metrics_required");
    }
    const ids = new Set();
    const metrics = source.map(function (raw, index) {
      const item = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
      const id = boundedText(item.id, `metrics[${index}].id`, 80);
      if (ids.has(id)) {
        throw new SituationProjectionError("scalar_metric_duplicate", id);
      }
      ids.add(id);
      const value = finiteNumber(item.value, `metrics[${index}].value`);
      const trend = String(item.trend || "").trim().toLowerCase();
      if (!["rising", "falling", "steady"].includes(trend)) {
        throw new SituationProjectionError("scalar_trend_invalid", id);
      }
      if (!Array.isArray(item.safe) || item.safe.length !== 2) {
        throw new SituationProjectionError("scalar_safe_range_required", id);
      }
      const low = finiteNumber(item.safe[0], `metrics[${index}].safe[0]`);
      const high = finiteNumber(item.safe[1], `metrics[${index}].safe[1]`);
      if (low > high) {
        throw new SituationProjectionError("scalar_safe_range_invalid", id);
      }
      return {
        id: id,
        label: boundedText(item.label, `metrics[${index}].label`, 120),
        value: value,
        unit: boundedText(item.unit, `metrics[${index}].unit`, 40),
        trend: trend,
        safe: [low, high],
      };
    });
    return deepFreeze({kind: "scalars/v1", metrics: metrics});
  }

  function sequenceSituation(options) {
    const config = options && typeof options === "object" ? options : {};
    const source = Array.isArray(config.steps) ? config.steps : null;
    if (!source || source.length === 0) {
      throw new SituationProjectionError("sequence_steps_required");
    }
    if (source.length > 64) {
      throw new SituationProjectionError("sequence_steps_too_many", source.length);
    }
    const ids = new Set();
    const steps = source.map(function (raw, index) {
      const item = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
      const id = boundedText(item.id, `steps[${index}].id`, 80);
      if (ids.has(id)) {
        throw new SituationProjectionError("sequence_step_duplicate", id);
      }
      ids.add(id);
      return {
        id: id,
        label: boundedText(item.label, `steps[${index}].label`, 120),
      };
    });
    const completedCount = Number(config.completedCount);
    if (
      !Number.isInteger(completedCount)
      || completedCount < 0
      || completedCount > steps.length
    ) {
      throw new SituationProjectionError(
        "sequence_completed_count_invalid",
        config.completedCount
      );
    }
    return deepFreeze({
      kind: "sequence/v1",
      completedCount: completedCount,
      nextStepId: completedCount < steps.length ? steps[completedCount].id : null,
      steps: steps,
    });
  }

  function controllerSituation(options) {
    const config = options && typeof options === "object" ? options : {};
    const status = String(config.status || "").trim().toLowerCase();
    if (!["idle", "active", "stopping", "blocked"].includes(status)) {
      throw new SituationProjectionError("controller_status_invalid", status);
    }
    const idle = status === "idle";
    const policyRevision = config.policyRevision;
    const policyAction = config.policyAction;
    const policySummary = config.policySummary;
    if (idle) {
      if (policyRevision !== null && policyRevision !== undefined) {
        throw new SituationProjectionError("controller_idle_policy_invalid");
      }
      if (policyAction !== null && policyAction !== undefined && policyAction !== "") {
        throw new SituationProjectionError("controller_idle_policy_invalid");
      }
      if (policySummary !== null && policySummary !== undefined && policySummary !== "") {
        throw new SituationProjectionError("controller_idle_policy_invalid");
      }
    } else if (!Number.isInteger(Number(policyRevision)) || Number(policyRevision) < 0) {
      throw new SituationProjectionError("controller_policy_revision_invalid");
    }
    const result = {
      kind: "controller/v1",
      status: status,
      policyRevision: idle ? null : Number(policyRevision),
      policyAction: idle ? null : semanticType(policyAction, "policyAction"),
      policySummary: idle ? "" : boundedText(policySummary, "policySummary", 240),
    };
    if (config.reason !== null && config.reason !== undefined && config.reason !== "") {
      result.reason = boundedText(config.reason, "reason", 160);
    }
    return deepFreeze(result);
  }

  function normalizeLegend(value, empty) {
    const source = value && typeof value === "object" && !Array.isArray(value)
      ? value
      : {};
    const result = {};
    Object.keys(source).sort().forEach(function (rawSymbol) {
      const symbol = oneSymbol(rawSymbol, "legend symbol");
      const label = String(source[rawSymbol] || "").replace(/\s+/g, " ").trim();
      if (!label) throw new SituationProjectionError("grid_legend_label_required", symbol);
      if (symbol === empty) {
        throw new SituationProjectionError("grid_empty_symbol_in_legend", symbol);
      }
      result[symbol] = label.slice(0, 80);
    });
    return result;
  }

  function positiveInteger(value, name) {
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed <= 0) {
      throw new SituationProjectionError("grid_dimension_invalid", name);
    }
    return parsed;
  }

  function oneSymbol(value, name) {
    const symbol = String(value === null || value === undefined ? "" : value);
    // grid/v1 promises direct rows[y][x] lookup across JavaScript and JSON
    // consumers, so one cell is exactly one UTF-16 code unit.
    if (symbol.length !== 1) {
      throw new SituationProjectionError("grid_symbol_invalid", name);
    }
    return symbol;
  }

  function boundedText(value, name, limit) {
    const text = String(value === null || value === undefined ? "" : value)
      .replace(/\s+/g, " ")
      .trim();
    if (!text) throw new SituationProjectionError("situation_text_required", name);
    if (text.length > limit) {
      throw new SituationProjectionError("situation_text_too_long", name);
    }
    return text;
  }

  function semanticType(value, name) {
    const text = boundedText(value, name, 120).toLowerCase();
    if (!/^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$/.test(text)) {
      throw new SituationProjectionError("choice_action_invalid", name);
    }
    return text;
  }

  function finiteNumber(value, name) {
    const number = Number(value);
    if (!Number.isFinite(number)) {
      throw new SituationProjectionError("scalar_number_invalid", name);
    }
    return number;
  }

  function jsonObjectClone(value, name) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new SituationProjectionError("choice_payload_invalid", name);
    }
    try {
      const encoded = JSON.stringify(value, function (_key, item) {
        if (
          item === undefined
          || typeof item === "function"
          || typeof item === "symbol"
          || typeof item === "bigint"
          || (typeof item === "number" && !Number.isFinite(item))
        ) {
          throw new TypeError("non-json payload value");
        }
        return item;
      });
      const clone = JSON.parse(encoded);
      if (!clone || typeof clone !== "object" || Array.isArray(clone)) {
        throw new TypeError("payload is not an object");
      }
      return clone;
    } catch (_error) {
      throw new SituationProjectionError("choice_payload_invalid", name);
    }
  }

  function deepFreeze(value) {
    if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
    Object.keys(value).forEach(function (key) { deepFreeze(value[key]); });
    return Object.freeze(value);
  }

  return Object.freeze({
    gridSituation: gridSituation,
    choiceSituation: choiceSituation,
    actionAvailabilitySituation: actionAvailabilitySituation,
    scalarSituation: scalarSituation,
    sequenceSituation: sequenceSituation,
    controllerSituation: controllerSituation,
    SituationProjectionError: SituationProjectionError,
  });
});
