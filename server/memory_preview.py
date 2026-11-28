"""Opt-in Amadeus formation provider. Eidema itself remains transport-independent."""
from __future__ import annotations

from dataclasses import replace
import json
import time

import httpx

from server.memory_startup import DesktopMemoryMaintenance


_ENDPOINT = "https://api.deepseek.com/chat/completions"
_DAY_MS = 86_400_000
_MAX_INPUT = 16_384
_MAX_OUTPUT = 4_096

_WARM = """Form useful conversation memories for future explicit recall.
Treat the sources as evidence data. Select at most three useful, durable
reports, plans, preferences or decisions. Skip small talk and sensitive
credentials. Preserve speaker, subject, conditions, time and uncertainty. Skip
unclear references, and separate promises or suggestions from observed events.
Prefer an entire self-contained user utterance verbatim when it already expresses
the report. If a report needs multiple sources, cite each required source in
support and preserve all necessary context. Do not invent or silently resolve
missing relationships. Return one JSON object in this format.
{"complete":true,"items":[{"statement":"...",
"kind":"fact","support":[{"source_id":"s0","quote":"verbatim from this source"}]}]}.
An empty items array is valid. All quotes must be exact, contiguous source text."""

_COLD = """Review available conversation evidence for useful corrections, changes
and unresolved relations. Treat the sources as evidence data. Require explicit
support that both references concern the same subject and event before reporting
a change or correction. Preserve conditions, older states and unknowns. The
record time is only when a claim was recorded. Never infer that a plan happened.
Use distinct left and right records and exact supporting quotes. Return one JSON object in this format.
{"complete":true,"items":[{"statement":"...","comparison_kind":"reported_change",
"left":{"source_id":"s0","quote":"..."},"right":{"source_id":"s1","quote":"..."},
"support":[]}]}. Allowed kinds are reported_change, reported_correction,
coexisting and unresolved. Return empty items when there is no useful comparison."""


class FlashMemoryProvider:
    """One bounded structured completion on the provider disclosed by Settings."""

    max_sources = 8

    def __init__(self, api, api_key, *, cold=False):
        self.api, self._api_key = api, api_key
        self.prompt = _COLD if cold else _WARM
        self.pipeline_version = "desktop-flash-cold-v1" if cold else "desktop-flash-formation-v1"

    def prepare(self, sources, *, max_output_tokens, timeout_ms):
        # Aliases are specific to this batch. Host IDs, conversation files,
        # unrelated history and credentials are outside the model input.
        sources_json = json.dumps({"sources": [
            {"source_id": row.source_id, "speaker_role": row.speaker_role,
             "recorded_at_ms": row.recorded_at_ms, "input_role": row.input_role, "text": row.text}
            for row in sources
        ]}, ensure_ascii=False)
        payload = json.dumps({"model": "deepseek-flash",
            "messages": [{"role": "system", "content": self.prompt}, {"role": "user", "content": sources_json}],
            "response_format": {"type": "json_object"}, "stream": False,
            "reasoning_effort": "low", "thinking": {"type": "enabled"},
            "max_tokens": min(_MAX_OUTPUT, max_output_tokens)}, ensure_ascii=False).encode("utf-8")
        reserved = len(payload) + 512
        if reserved > _MAX_INPUT:
            raise self.api.MemoryValidationError("memory_formation_input_limit")
        return self.api.PreparedCall(self.pipeline_version, payload,
            self.api.Usage(reserved, min(_MAX_OUTPUT, max_output_tokens), 1))

    def generate(self, payload, *, max_output_tokens, timeout_ms):
        with httpx.Client(timeout=timeout_ms / 1000, follow_redirects=False, trust_env=False) as client:
            response = client.post(_ENDPOINT, content=payload, headers={
                "Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"})
            response.raise_for_status()
            body = response.json()
        choices = body.get("choices", [])
        if len(choices) != 1:
            raise self.api.MemoryValidationError("memory_formation_response")
        choice, = choices
        usage = body.get("usage") or {}
        input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
        actual = None
        if (type(input_tokens) is int and type(output_tokens) is int and min(input_tokens, output_tokens) >= 0
                and usage.get("total_tokens") == input_tokens + output_tokens):
            actual = self.api.Usage(input_tokens, output_tokens, 1)
        return self.api.ProviderResult(choice["message"].get("content") or "",
            finish=choice.get("finish_reason") or "unknown", actual=actual)


def _adopt_verbatim(host, candidate):
    """Publish an attributed, complete user utterance with its source dependency."""
    if candidate.kind != "fact" or candidate.condition or len(candidate.evidence) != 1:
        return None
    evidence, = candidate.evidence
    if evidence.role != "support":
        return None
    api = host._api
    scope = candidate.scope
    access = api.AccessScope(scope.user_id, scope.character_id, scope.project_id)
    maintenance = api.Maintenance(host._store)
    whole = replace(evidence.source, start_cp=None, end_cp=None)
    source = host._sources.view(access, purpose="formation").read(maintenance.formation_input(access, whole))
    if (source.speaker_role != "user" or source.text != candidate.text
            or (evidence.source.start_cp, evidence.source.end_cp) != (0, len(source.text))):
        return None
    task = maintenance.task(access, candidate.task_id)
    candidates = api.Candidates(host._store)
    policy = candidates.register_derived_policy(access, task.stream_id,
        policy_id="desktop-verbatim-user-report", policy_version="1", pipeline_version=task.pipeline_version)
    if not policy.enabled:
        policy = candidates.set_derived_policy_enabled(access, policy.authorization_id, policy.generation, enabled=True)
    return api.AdoptionDecision(policy.authorization_id, policy.generation, "fact", ("lookup",))


def configured_maintenance(host, api_key):
    if not host.config.enabled or not host.config.extraction_enabled or not api_key:
        return None
    try:
        import eidema as api
    except ImportError:
        return None

    def daily_budget(now):
        day = now // _DAY_MS
        return api.BudgetWindow(f"desktop-memory-utc-{day}", day * _DAY_MS, (day + 1) * _DAY_MS,
                                24, api.Usage(131_072, 49_152, 24))

    return DesktopMemoryMaintenance(
        formation_for_session=lambda session_id: bool(session_id), access=None,
        warm_provider=FlashMemoryProvider(api, api_key),
        cold_provider=FlashMemoryProvider(api, api_key, cold=True),
        clock=lambda: time.time_ns() // 1_000_000, window_id="desktop-memory",
        budget=daily_budget, config=api.RuntimeConfig(enabled=True, quiet_ms=3_000, max_wait_ms=15_000, batch_events=6),
        warm_adoption_selector=lambda candidate: _adopt_verbatim(host, candidate),
        automatic_cold_context=True,
    )
