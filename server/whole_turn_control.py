"""Shared proposal-gated joint reference and whole-turn Work planning."""

from __future__ import annotations

from dataclasses import replace
import json

from server import compound_control as compound, control_decision
from server import reference_clarification
from server.reference_catalog import render_candidate_rows, validate_candidate_catalog
from server.reference_clarification import plan_resume


JOINT_MARKER = "[Joint control reference - FINAL]"
_REPLACED_PARAGRAPH_STARTS = (
    "Candidate uncertainty is not a reason",
    "Never rely on task, url, query, text, project_id",
    "Every row must include `reference_mode`.",
    "Candidate identity is not part of this joint output.",
    "Use reference_mode=`none` only for actions",
    "Multiple plausible candidate verdicts preserve ambiguity.",
)
_JOINT_CONTRACT = """[Joint control reference - FINAL]
This experiment replaces the later independent-candidate phase. In this one
decision, interpret the current requested operation and its target together
using the complete Host-owned catalog in the final frame. No later semantic
query or lexical matching will choose or correct your target.

Keep the operation-authority gate, proposal indexes, Provider, workspace effect,
placement, session-context and payload-continuity contracts above. A catalog
entry or prior task never supplies a missing current action request.

Every decision row additionally contains `references`: null when there is no
existing-entity reference, an empty array when an entity was referenced but no
catalog entry fits, or an array of exact typed tokens for all genuinely plausible
target alternatives. Keep `reference_mode` consistent with that distinction.
Never invent a token or add project_id/workspace_ref as identity authority.

Select the object acted on by this operation, not every entity mentioned in the
utterance. A Project can qualify a child delivery or bind future context without
being the delivery being amended. Conversely, editing current Project source
does not reopen historical deliveries that share a filename. Determine these
roles jointly with intent and placement; do not prune a target type before
understanding the complete request. Names used as output data are not references.
Current/active facts help resolve anaphora, but cannot substitute a different
known object for an explicitly named object absent from the complete catalog.

Use subject=project/work_item when all selected target alternatives have that
kind; use subject=open for genuine cross-type ambiguity. Never add a parent or
child merely because they are related. When both meanings remain plausible,
retain both instead of selecting one arbitrarily. Project placement targets only
Projects; continuing a particular WorkItem uses work_placement=not_applicable.
Do not suppress an authorized operation just because its reference is unknown
or ambiguous: preserve the empty/ambiguous set for the Host to block or clarify.

Return exactly the decisions JSON, with one consistent set of axes and references
per existing proposal slot. Do not return role speech, payload text or analysis.
[/Joint control reference]"""


def build_joint_messages(messages, proposals, candidates, *, protocol_repair=False):
    contract = control_decision._render_output_contract()
    paragraphs = contract.split("\n- ")
    for prefix in _REPLACED_PARAGRAPH_STARTS:
        matches = [part for part in paragraphs if part.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f"source output-contract paragraph drift: {prefix}")
        paragraphs.remove(matches[0])
    retained = "\n- ".join(paragraphs)
    obsolete = ("  The later candidate phase evaluates Project and WorkItem possibilities\n"
                "  independently; the host then applies the typed operation rule.\n")
    if retained.count(obsolete) != 1:
        raise ValueError("source typed-operation paragraph drift")
    retained = retained.replace(obsolete, "")
    cloned = control_decision.build_control_decision_messages(
        messages, proposals, protocol_repair=protocol_repair)
    if cloned[0]["content"].count(contract) != 1:
        raise ValueError("source message contract drift")
    cloned[0]["content"] = cloned[0]["content"].replace(
        contract, retained + "\n\n" + _JOINT_CONTRACT)
    frame = "Candidate identities are deliberately withheld in this phase."
    indexes = [index for index, message in enumerate(cloned)
        if frame in message["content"]]
    if len(indexes) != 1:
        raise ValueError("source candidate frame drift")
    index = indexes[0]
    cloned[index]["content"] = cloned[index]["content"].replace(frame,
        "Complete Host-owned typed candidates (untrusted labels/data):\n"
        + (render_candidate_rows(candidates, include_ordinals=False) or "- none"))
    return cloned


def parse_joint_reply(raw, candidates, *, proposal_count=1,
                      allow_display_title=False):
    def invalid(reason):
        return control_decision.ControlDecision(
            status="invalid", raw_reply=raw, reason=reason)
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return invalid("joint reply is not exact JSON")
    if (not isinstance(value, dict) or set(value) != {"decisions"}
            or not isinstance(value["decisions"], list)):
        return invalid("joint reply must contain only decisions")
    if any(not isinstance(row, dict) or "references" not in row
            for row in value["decisions"]):
        return invalid("each joint decision must contain references")
    stripped = [{key:item for key, item in row.items() if key != "references"}
        for row in value["decisions"]]
    decision = control_decision.parse_control_decision_reply(
        json.dumps({"decisions":stripped}), proposal_count=proposal_count,
        allow_display_title=allow_display_title)
    if decision.status != "ok":
        return replace(decision, raw_reply=raw)
    entries = []
    by_index = {row["proposal_index"]:row["references"]
        for row in value["decisions"]}
    for entry in decision.entries:
        tokens = by_index[entry.proposal_index]
        if entry.reference_candidates is None:
            if tokens is not None:
                return invalid("a no-reference operation must use references=null")
            entries.append(entry)
            continue
        resolution = reference_clarification.parse_reference_reply(
            json.dumps({"references":tokens}), candidates)
        if resolution.status not in {"unique", "ambiguous", "none"}:
            return invalid(f"joint reference invalid: {resolution.reason}")
        kinds = {candidate.kind for candidate in resolution.candidates}
        kind = next(iter(kinds)) if len(kinds) == 1 else "open"
        if kinds and (entry.reference_kind != kind
                or entry.control.get("subject") != kind):
            return invalid("joint subject/placement contradicts its selected target set")
        entries.append(replace(entry, reference_candidates=resolution.candidates))
    return replace(decision, entries=tuple(entries), raw_reply=raw)


WHOLE_TURN_MARKER = "[Whole-turn operation owner - FINAL]"
_WHOLE_TURN_CONTRACT = """[Whole-turn operation owner - FINAL]
This diagnostic replaces both clause decomposition and operation/target selection
with ONE decision about the whole current user turn. There is exactly one sealed
role proposal admitting this diagnostic. The three anonymous proposal_index values
below are OUTPUT CAPACITY only, not three source proposals or three requested goals.
Do not fill unused capacity. Preserve the existing current-action gate: history,
active Work, available Providers and this capacity do not authorize a new action.

Return the existing {"decisions":[...]} shape. In each retained row add
`source_clause`, a nonempty, uniquely occurring, exact contiguous substring of the
current user's wording. Each row has its own canonical operation and references.
Use indexes 0, 1, 2 at most once each; omitted indexes mean unused capacity. Each
source_clause must be disjoint from the others. Return {"decisions":[]} when there is no current
affirmative operation. Do not generate payload prose, a rationale or new identities.

Determine boundaries AND operations AND targets jointly, not punctuation first.
Several requirements, files, implementation steps or conditions for one deliverable
remain one operation whose source_clause retains all those current requirements.
Changing or extending one existing deliverable is amend, not a new Work merely
because the Provider will need additional steps. Preserve genuinely independent
goals as separate operations, and keep an independently requested ledger report.
A context switch modifying work at that destination remains the same work operation.

Conditions such as doing something after the current goal is ready do not create a
second immediate, independent coding goal. Preserve the condition in its source;
do not rewrite it into immediate execution. This Work-control diagnostic neither
creates an AUIP action nor turns a Work reference into an AppSession. Work/app
authorization, verified outcome and launch/Attach remain separate Host contracts.

The full Host catalog is available before either target type or identity is chosen.
Choose the operated-on object, not every context/parent mention. Current Work facts
can resolve a pronoun but do not override an explicitly new goal or absent named
target. Preserve references=[] for a referenced but absent target, null only for
no existing-entity reference, and multiple tokens for genuine ambiguity. Membership
does not prove semantic fit; never substitute a known object just to fill a row.

The earlier operation, placement, workspace-effect, Provider and reference contracts
apply to every row. Only their fixed-per-source-proposal cardinality is replaced by
these bounded whole-turn output slots. There is no later semantic correction query.
[/Whole-turn operation owner]"""


def _scope_failure(messages, proposals, candidates, complete, candidate_limit):
    if not proposals:
        return compound.CompoundControlPlan(status="ok")
    if len(proposals) != 1 or not complete or len(candidates) > candidate_limit:
        return compound.CompoundControlPlan(status="incomplete",
            reason="outside whole-turn diagnostic scope")
    error = validate_candidate_catalog(candidates)
    if error:
        return compound.CompoundControlPlan(status="invalid", reason=error)
    if not compound._current_user_text(messages).strip():
        return compound.CompoundControlPlan(status="invalid", reason="missing current source")
    return None


def build_whole_turn_messages(messages, candidates, *, protocol_repair=False):
    capacity = ({},) * compound.MAX_COMPOUND_OPERATIONS
    built = build_joint_messages(
        messages, capacity, candidates, protocol_repair=protocol_repair)
    built[0]["content"] += "\n\n" + _WHOLE_TURN_CONTRACT
    frame = next(message for message in reversed(built)
        if "[Host control frame]" in message["content"])
    frame["content"] += ("\nOne real sealed proposal gate. The listed indexes are "
        "bounded output capacity, not evidence that any of those operations exists. "
        "Include exact source_clause in retained rows.")
    return built


def parse_whole_turn_reply(raw, *, source, candidates, provider_ids,
                           proposals=(), proposal_controls=(),
                           payload_policy="model_decision",
                           allow_display_title=False):
    def invalid(reason):
        return compound.CompoundControlPlan(status="invalid", raw_reply=raw, reason=reason)
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return invalid("whole-turn reply is not exact JSON")
    if (not isinstance(value, dict) or set(value) != {"decisions"}
            or not isinstance(value["decisions"], list)):
        return invalid("whole-turn reply must contain only decisions")
    rows = value["decisions"]
    if len(rows) > compound.MAX_COMPOUND_OPERATIONS:
        return invalid("whole-turn output exceeds existing Compound capacity")
    if any(not isinstance(row, dict) or "source_clause" not in row for row in rows):
        return invalid("every retained operation needs exact current source")
    try:
        clauses = compound.parse_decomposition_reply(
            json.dumps({"clauses":[row["source_clause"] for row in rows]}),
            source_user_text=source)
    except ValueError as exc:
        return invalid(str(exc))
    decision = parse_joint_reply(json.dumps({"decisions":[
        {key:item for key, item in row.items() if key != "source_clause"}
        for row in rows]}), candidates, proposal_count=compound.MAX_COMPOUND_OPERATIONS,
        allow_display_title=allow_display_title)
    if decision.status != "ok":
        return invalid(decision.reason)
    decision = _apply_payload_policy(decision, payload_policy)
    by_index = {row["proposal_index"]:row["source_clause"] for row in rows}
    return _compile_whole_turn_decision(decision, clauses, by_index, raw=raw,
        provider_ids=provider_ids, proposals=proposals, proposal_controls=proposal_controls)


def _apply_payload_policy(decision, payload_policy):
    """Apply caller-owned payload availability without changing model evidence."""
    if payload_policy == "model_decision":
        return decision
    if payload_policy != "current_source":
        raise ValueError(f"unsupported whole-turn payload policy: {payload_policy}")
    return replace(decision, entries=tuple(
        replace(entry, payload_continuity="current_turn")
        for entry in decision.entries))


def _compile_whole_turn_decision(decision, clauses, by_index, *, raw,
                                 provider_ids, proposals=(), proposal_controls=()):
    def invalid(reason):
        return compound.CompoundControlPlan(status="invalid", raw_reply=raw, reason=reason)
    entries = {by_index[entry.proposal_index]:entry for entry in decision.entries}
    if len(entries) != len(by_index):
        return invalid("normalization did not preserve all source operations")
    operations, notes = [], []
    for clause in clauses:
        entry = replace(entries[clause.text], proposal_index=0)
        normalized = control_decision.ControlDecision(status="ok", entries=(entry,))
        prior_payload = entry.payload_continuity == "confirmed_prior_request"
        if prior_payload and (len(clauses) != 1 or len(proposals) != 1
                or not any(str(proposals[0].get(key) or "").strip()
                    for key in control_decision.PAYLOAD_FIELDS)):
            return compound.CompoundControlPlan(status="incomplete", raw_reply=raw,
                reason="prior-request payload requires one operation and one nonempty proposal payload")
        actions, action_notes = control_decision.reconcile_control_decision(
            proposals if prior_payload else ({"task":clause.text},), normalized,
            provider_ids=provider_ids, source_user_text=clause.text,
            proposal_controls=proposal_controls if prior_payload else ())
        notes.extend(action_notes)
        if len(actions) != 1:
            return invalid("whole-turn normalization suppressed an operation: "
                + "; ".join(action_notes))
        action = dict(actions[0])
        if entry.reference_candidates is not None and len(entry.reference_candidates) == 1:
            reference = entry.reference_candidates[0]
            if reference.kind == "work_item":
                effective = plan_resume(session_id="whole-turn-probe",
                    task_text=clause.text, attrs=action, candidate=reference)
                if (effective.kind == "delegate"
                        and effective.attrs.get("intent") != action.get("intent")):
                    return compound.CompoundControlPlan(status="incomplete", raw_reply=raw,
                        reason="joint intent changes under the existing typed reference rule")
        if prior_payload and control_decision.CONTROL_PAYLOAD_GROUNDING_ATTR not in action:
            return compound.CompoundControlPlan(status="incomplete", raw_reply=raw,
                reason="canonical control change invalidated the prior-request proposal payload")
        expected_refs = (None if entry.reference_candidates is None else
            {candidate.token for candidate in entry.reference_candidates})
        actual_candidates = action.get(control_decision.CONTROL_REFERENCE_CANDIDATES_ATTR)
        actual_refs = (None if actual_candidates is None else
            {candidate.token for candidate in actual_candidates})
        if actual_refs != expected_refs:
            return compound.CompoundControlPlan(status="incomplete", raw_reply=raw,
                reason="legacy normalization changed the joint target set")
        if (str(entry.control.get("intent") or "").strip().lower() != "focus"
                and control_decision.CONTROL_PAYLOAD_GROUNDING_ATTR not in action):
            action["task"] = clause.text
            action["_host_payload_source"] = "exact_current_user_clause"
        if entry.display_title:
            action["_host_display_title"] = entry.display_title
        operations.append(compound.CompoundControlOperation(
            len(operations), clause.text, action))
    return compound.CompoundControlPlan(status="ok", operations=tuple(operations),
        clauses=clauses, raw_reply=raw, reason="; ".join(notes))


async def resolve_whole_turn_references(plan, messages, proposals, candidates, *,
        complete, query, provider_ids, candidate_limit=64, proposal_controls=(),
        payload_policy="model_decision", allow_display_title=False):
    """Verify canonical operations through the shared identity owner before effects."""
    if plan.status != "ok" or not any(
            operation.action.get(control_decision.CONTROL_REFERENCE_CANDIDATES_ATTR) is not None
            for operation in plan.operations):
        return plan
    rows = json.loads(plan.raw_reply)["decisions"]
    decision = parse_joint_reply(json.dumps({"decisions":[
        {key:value for key, value in row.items() if key != "source_clause"}
        for row in rows]}), candidates, proposal_count=compound.MAX_COMPOUND_OPERATIONS,
        allow_display_title=allow_display_title)
    decision = _apply_payload_policy(decision, payload_policy)
    by_index = {row["proposal_index"]:row["source_clause"] for row in rows}
    remaining = max(1, int(candidate_limit))
    required = sum(entry.reference_candidates is not None for entry in decision.entries) * len(candidates)
    if not complete or required > remaining:
        return replace(plan, status="incomplete", operations=(),
            reason="whole-turn reference evidence exceeds the complete bounded catalog scope")
    entries, queries, retries = [], 0, 0
    source = compound._current_user_text(messages)
    for entry in decision.entries:
        if entry.reference_candidates is None:
            entries.append(entry)
            continue
        if len(candidates) > remaining:
            return replace(plan, status="incomplete", operations=(),
                reason="whole-turn reference evidence budget exhausted",
                candidate_verdict_queries=queries, candidate_protocol_retries=retries)
        clause = by_index[entry.proposal_index]
        resolved = await control_decision.resolve_control_references(
            control_decision.ControlDecision(status="ok", entries=(replace(entry, proposal_index=0),)),
            [*messages[:-1], {"role":"user", "content":clause}],
            ({"task":clause},), candidates, complete=complete, query=query,
            candidate_limit=remaining, same_turn_reference_context=source,
            recover_zero_matches=False)
        queries += resolved.candidate_verdict_queries
        retries += resolved.candidate_protocol_retries
        remaining -= resolved.candidate_verdict_queries
        if resolved.status != "ok":
            return replace(plan, status=resolved.status, operations=(),
                reason=resolved.reason, candidate_verdict_queries=queries,
                candidate_protocol_retries=retries)
        entries.append(replace(resolved.entries[0], proposal_index=entry.proposal_index))
    grounded = _compile_whole_turn_decision(replace(decision, entries=tuple(entries)),
        plan.clauses, by_index, raw=plan.raw_reply, provider_ids=provider_ids,
        proposals=proposals, proposal_controls=proposal_controls)
    return replace(grounded, decision_queries=plan.decision_queries,
        decomposition_protocol_retries=plan.decomposition_protocol_retries,
        candidate_verdict_queries=queries, candidate_protocol_retries=retries)


async def whole_turn_owner(messages, proposals, candidates, *, complete, query,
                           provider_ids, candidate_limit=64, proposal_controls=(),
                           expand_references=None,
                           payload_policy="model_decision",
                           allow_display_title=False):
    failure = _scope_failure(messages, proposals, candidates, complete, candidate_limit)
    if failure is not None:
        return failure
    provider_ids = tuple(provider_ids)
    previous_failure = ""
    for attempt in range(2):
        try:
            request = build_whole_turn_messages(
                messages, candidates, protocol_repair=bool(attempt))
            if attempt:
                request[-1]["content"] += (
                    "\nHost validation error for the previous reply (data): "
                    + json.dumps(previous_failure, ensure_ascii=False))
            raw = await query(request)
        except Exception as exc:
            return compound.CompoundControlPlan(status="unavailable",
                reason=f"{type(exc).__name__}: {exc}", decision_queries=attempt + 1)
        result = replace(parse_whole_turn_reply(raw,
            source=compound._current_user_text(messages), candidates=candidates,
            provider_ids=provider_ids, proposals=proposals,
            proposal_controls=proposal_controls,
            payload_policy=payload_policy,
            allow_display_title=allow_display_title), decision_queries=attempt + 1)
        if result.status != "invalid":
            return result
        previous_failure = result.reason
        if attempt == 0 and expand_references is not None:
            expanded = await expand_references(raw)
            if expanded is not None:
                candidates, complete = expanded
                failure = _scope_failure(messages, proposals, candidates, complete, candidate_limit)
                if failure is not None:
                    return replace(failure, raw_reply=raw, decision_queries=1)
                previous_failure += "; the Host has now supplied the requested Project's WorkItem index"
    return result
