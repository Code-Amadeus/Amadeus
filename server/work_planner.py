"""Runtime professional Work planning behind one coarse role proposal."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import inspect
import json
import logging
from typing import Callable, Mapping

from server.control_adjudication import RuntimeControlDecisionResolver
from server.control_proposal import seal_control_proposals
from server.focus_policy import finalize_work_focus_modifiers
from server.whole_turn_control import whole_turn_owner, resolve_whole_turn_references
from server.work_planner_prompt import get_work_planner_prompt, project_work_planner_messages
from server.reference_catalog import candidate_catalog_from_coordinator


APP_WORK_SCOPE = (
    "この判断が担当するのはProviderへの独立したWork依頼です。既に開いているアプリの公開機能を使う要求は"
    "AUIPに属し、アプリの状態や保存データが変わっても、そのアプリを作ったWorkへのamendを重ねません。"
    "アプリの機能追加・修正や成果物そのものの作成・編集はWorkです。原文に両方あればWorkの部分だけを保持します。"
    "AUIPの判断は参考情報であり、別に依頼されたWorkを取り消す権限はありません。"
)

ENTRY_DISCOVERY_SCOPE = (
    "Hostの管理アプリ索引に直接使える入口が無く、今回の要求はまだ実行されていません。"
    "現在の原文がその対象を探して開く・使う依頼なら、発見と元の目的を進める独立Workをexecute/draftで受け付けます。"
    "検索対象の名前は調査目標であり、特定済みWork参照ではありません。URLやProjectの事前指定は不要です。"
    "既存成果を作り直して代用したり、対象未特定のまま他の成果を変更・停止したりしません。"
)

CURRENT_WORK_FACTS_SCOPE = (
    "これはHostが現在の会話宛先で最後に受理したcurrent_workの事実です。"
    "execution_statusはProvider実行の状態であり、done/succeededでもWorkのGoal完了を証明しません。"
    "Workの達成状況はcompletenessとattentionを含むHost事実から区別してください。"
    "current_workは省略や返答先を解釈するための文脈であり、現在の発話の強制targetや新しい実行許可ではありません。"
    "input_requirementsがある場合、それは既に投影された追加要求と配信状態の事実です。"
    "providerはそのWorkの実際のProvider事実であり、既定Providerから推測した値ではありません。"
)

PROVIDER_MESSAGE_SCOPE = (
    "役割はProvider会話へのメッセージを提案しています。まだ会話先が無い提案も含むため、"
    "まず原文全体から今回直接求められた行為を区別し、Workに該当する部分にだけWorkのintentと着手確認の規則を適用します。"
    "実行者への質問・相談・説明の続きなど、交付の実行ではなく応答を求める行為はintent=messageです。"
    "元の正確なtyped候補をsubject/referencesに保持し、work_placement=not_applicable、"
    "session_context=unchanged、workspace_effect=noneを使います。"
    "話題が進捗や残作業でも、台帳で似た答えが出せることを理由にreportへ置き換えません。"
    "誰に何を求めたかを変えるように原文の主句を落としてはいけません。"
    "会話の続きを求める同意は、Workの着手同意ではありません。"
    "有効なmessage結果だけをHostが元のメッセージownerへ渡します。"
    "未完成の交付の実行継続・再開、独立した調査、新しい成果、既存Workの要件を確定・変更する依頼は、"
    "通常のcanonical execute/amend規則でWorkとして返してください。"
)

_CURRENT_WORK_FACT_FIELDS = (
    "work_item_id",
    "goal",
    "provider",
    "execution_status",
    "work_state",
    "completeness",
    "attention",
    "input_requirements",
)


def _snapshot_current_work(loop) -> dict | None:
    """Freeze only the existing recipient Work projection before model I/O."""

    reader = getattr(loop, "recipient_work", None)
    if not callable(reader):
        return None
    value = reader(loop.bound_context_id)
    if not isinstance(value, Mapping) or not str(value.get("work_item_id") or "").strip():
        return None
    return {
        field: deepcopy(value[field])
        for field in _CURRENT_WORK_FACT_FIELDS
        if field in value
    }


def _render_current_work_facts(facts: Mapping) -> str:
    return (
        "\n\n[今回のcurrent_work Host事実]\n"
        + json.dumps(dict(facts), ensure_ascii=False, separators=(",", ":"))
        + "\n"
        + CURRENT_WORK_FACTS_SCOPE
        + "\n[/今回のcurrent_work Host事実]"
    )


class RuntimeWorkPlanner:
    def __init__(self, *, coordinator, query: Callable, provider: str,
                 project_limit: int = 200, work_item_limit: int = 200,
                 candidate_limit: int = 64):
        if not callable(query):
            raise TypeError("Runtime Work planner query must be callable")
        self.coordinator = coordinator
        self.query = query
        self.provider = str(provider or "").strip()
        if not self.provider:
            raise ValueError("Runtime Work planner requires a Provider")
        self.project_limit = max(1, int(project_limit))
        self.work_item_limit = max(1, int(work_item_limit))
        self.candidate_limit = max(1, int(candidate_limit))

    async def __call__(self, ingress, turn_id: str, receipt: dict, admission):
        source = str(receipt.get("source_user_text") or receipt.get("text") or "")
        prior = ingress.loop.prior_messages(turn_id)
        current_work_facts = _snapshot_current_work(ingress.loop)
        proposal = {"type":"DELEGATE", "attrs":{
            "provider":self.provider, "task":source},
            "raw":json.dumps({"action":{"op":"work"}}, ensure_ascii=False)}
        batch = seal_control_proposals((proposal,), turn_id=turn_id,
            session_id=ingress.session_id, user_text=source,
            transport="inline_tag", prior_messages=prior)
        auip = receipt.get("auip_context")
        auip_facts = ({key:auip[key] for key in (
            "action", "timing", "instruction", "app_session_id", "target", "project_ref", "reason") if key in auip}
            if isinstance(auip, dict) else {})

        resolver = RuntimeControlDecisionResolver(coordinator=self.coordinator,
            query=self.query, project_limit=self.project_limit,
            work_item_limit=self.work_item_limit,
            exhaustive_candidate_limit=self.candidate_limit)
        # Planning and dispatch must expose the same configured Work Providers.
        # The global registry also contains app/browser capabilities that this
        # cooperative Work entry cannot execute.
        provider_ids = frozenset(ingress.loop.context_requirements)
        semantic_prompt = get_work_planner_prompt(tuple(sorted(provider_ids)))
        if current_work_facts is not None:
            semantic_prompt += _render_current_work_facts(current_work_facts)
        provider_message = isinstance(receipt.get("provider_message_action"), Mapping)
        if provider_message:
            semantic_prompt += "\n\n" + PROVIDER_MESSAGE_SCOPE
        context = await asyncio.to_thread(resolver.capture_context, batch,
            include_app_capabilities=True, semantic_prompt=semantic_prompt)
        context = replace(context, provider_ids=provider_ids)
        messages = list(context.messages)
        if auip_facts:
            unresolved_entry = auip_facts.get("reason") == "entry_target_not_found"
            if unresolved_entry:
                # Pass the Host lookup result, not the superseded proposal to
                # engage an application that could not be found.
                auip_facts = {key:auip_facts[key] for key in (
                    "target", "project_ref", "reason") if key in auip_facts}
            label = ("Host入口検索結果・今回の要求は未実行" if unresolved_entry else
                "今回の既存AUIP判断（Hostが別途処理するアプリ側の要求）")
            messages[0] = {**messages[0], "content":messages[0]["content"]
                + "\n\n[" + label + "]\n"
                + json.dumps(auip_facts, ensure_ascii=False)
                + "\n" + (ENTRY_DISCOVERY_SCOPE if unresolved_entry else APP_WORK_SCOPE)
                + "\n[/" + label + "]"}

        async def query(query_messages):
            result = self.query(query_messages)
            return await result if inspect.isawaitable(result) else result

        async def planning_query(query_messages):
            # Reuse the shared current-source/catalog frame and protocol repair.
            # Its legacy layered system contracts are replaced by this strategy's
            # one professional contract plus the complete captured Host context.
            projected = project_work_planner_messages(query_messages,
                system=messages[0]["content"], source=source)
            return await query(projected)

        async def expand_references(raw):
            nonlocal context
            try:
                rows = json.loads(raw).get("decisions", [])
            except (TypeError, ValueError, AttributeError):
                return None
            if not isinstance(rows, list):
                return None
            projects = {candidate.token:candidate.entity_id for candidate in context.candidates
                if candidate.kind == "project"}
            requested = {projects[token] for row in rows if isinstance(row, dict)
                and row.get("subject") == "work_item" and isinstance(row.get("references"), list)
                for token in row["references"] if isinstance(token, str) and token in projects}
            if not requested:
                return None
            candidates, complete, reason = await asyncio.to_thread(candidate_catalog_from_coordinator,
                self.coordinator, ingress.session_id, project_limit=self.project_limit,
                work_item_limit=self.work_item_limit, indexed_project_ids=tuple(sorted(requested)))
            context = replace(context, candidates=candidates, catalog_complete=complete)
            logging.getLogger(__name__).info("[WORK-INDEX] turn=%s projects=%s complete=%s reason=%s",
                turn_id, sorted(requested), complete, reason)
            return candidates, complete

        plan = await whole_turn_owner(messages, batch.decision_payloads(),
            context.candidates, complete=context.catalog_complete,
            query=planning_query, provider_ids=context.provider_ids,
            candidate_limit=context.exhaustive_candidate_limit,
            proposal_controls=batch.proposals, expand_references=expand_references,
            payload_policy="current_source", allow_display_title=True)
        logging.getLogger(__name__).info("[WORK-PLAN] %s", json.dumps({
            "turn_id":turn_id, "status":plan.status, "reason":plan.reason,
            "raw_reply":plan.raw_reply, "operation_count":len(plan.operations),
        }, ensure_ascii=False, separators=(",", ":")))
        if plan.status != "ok":
            return plan
        plan = await resolve_whole_turn_references(plan, messages, batch.decision_payloads(),
            context.candidates, complete=context.catalog_complete, query=query,
            provider_ids=context.provider_ids, candidate_limit=context.exhaustive_candidate_limit,
            proposal_controls=batch.proposals, payload_policy="current_source",
            allow_display_title=True)
        if plan.status != "ok":
            return plan
        # Reuse the existing persistent-destination audit, after the role's
        # independent early delivery. Ordinary operations add no audit query.
        actions = [{"type":"DELEGATE", "attrs":dict(operation.action)}
            for operation in plan.operations]
        for action in actions:
            if action["attrs"].get("focus") and action["attrs"].get("intent") != "focus":
                action["attrs"]["_host_source_user_text"] = source
        await finalize_work_focus_modifiers(actions)
        for action in actions:
            # The existing focus owner uses legacy string spelling when a
            # denied CLEAR retains this task's independent Draft placement.
            if action["attrs"].get("one_off") == "true":
                action["attrs"]["one_off"] = True
        return replace(plan, operations=tuple(replace(operation, action=action["attrs"])
            for operation, action in zip(plan.operations, actions)))
