"""Legacy Mystery VN interpretation rules, isolated from the shared runtime."""

from __future__ import annotations

import re
from typing import Any

from .text import looks_like_topic_label, strip_vn_tags, text_hash

_PUNCT_RE = re.compile(r"[。！？!?…]")

_EVIDENCE_KEYWORDS = {
    "证据",
    "线索",
    "调查",
    "推理",
    "真相",
    "规则",
    "记录",
    "名字",
    "电话",
    "照片",
    "地图",
    "时间",
    "地点",
    "目击",
    "矛盾",
    "不对劲",
    "秘密",
    "理由",
    "动机",
    "真货",
    "实际存在",
    "能看到",
    "能够看到",
    "看得见",
    "看不见",
    "条件",
    "确凿",
    "诅咒珠",
    "咒主",
    "魂渣",
    "不留证据",
    "值得信赖",
}
_MYSTERY_KEYWORDS = {
    "死",
    "死亡",
    "杀",
    "尸体",
    "诅咒",
    "怨灵",
    "怪谈",
    "仪式",
    "复活",
    "人鱼",
    "失踪",
    "犯人",
    "嫌疑",
    "凶手",
    "命案",
    "危险",
    "救",
    "秘术",
    "本所七大不可思议",
    "置行堀",
    "通灵",
    "灵异",
    "杀人",
    "杀死",
    "咒主",
    "魂渣",
}
_AFFECT_KEYWORDS = {
    "没事",
    "不要",
    "等等",
    "为什么",
    "不可能",
    "对不起",
    "害怕",
    "可怕",
    "求你",
    "哭",
    "疯",
    "痛苦",
    "相信",
}
_CHOICE_KEYWORDS = {"选择", "怎么办", "怎么做", "该不该", "要不要", "决定", "选项"}


def _score_line(text: Any) -> tuple[float, str]:
    value = strip_vn_tags(str(text or ""))
    if not value:
        return 0.0, "low_density"
    if looks_like_topic_label(value):
        return 0.1, "topic_label"
    trailing_contrast_setup = _is_trailing_contrast_setup(value)
    score = 0.0
    kind = "low_density"
    if any(keyword in value for keyword in _EVIDENCE_KEYWORDS):
        score += 2.0
        kind = "new_evidence"
    if any(keyword in value for keyword in _MYSTERY_KEYWORDS):
        score += 2.4
        kind = "new_evidence" if kind == "low_density" else kind
    if any(keyword in value for keyword in _AFFECT_KEYWORDS):
        score += 1.4
        if kind == "low_density":
            kind = "emotional_beat"
    if any(keyword in value for keyword in _CHOICE_KEYWORDS):
        score += 2.0
        kind = "choice"
    if "？" in value or "?" in value:
        score += 0.9
    if "！" in value or "!" in value:
        score += 0.7
    if not trailing_contrast_setup and ("但是" in value or "可是" in value or "矛盾" in value or "不对" in value):
        score += 1.2
        kind = "contradiction"
    if "七大不可思议" in value and _has_count_anomaly_text(value):
        score += 2.2
        kind = "rule_anomaly"
    if len(_PUNCT_RE.findall(value)) >= 2:
        score += 0.4
    if len(value) <= 4:
        score *= 0.75
    return round(min(score, 5.0), 2), kind


def _fallback_speech(kind: str, language: str, line_text: Any = "") -> str:
    lang = str(language or "").lower()
    zh = lang.startswith("zh")
    ja = lang.startswith("ja") or "japanese" in lang
    text = strip_vn_tags(str(line_text or ""))
    if ja:
        if "复活" in text and any(marker in text for marker in {"只能", "使用1次", "使用１次", "一回", "一次"}):
            return "ちょっと待って。[EMO preset=thinking dur=8s] 復活を一回だけ使える、なんて前提を軽く出してきたわね。条件と代償を先に疑うべきよ。"
        if "复活秘术" in text or "复活" in text:
            return "復活の秘術、ね。[EMO preset=thinking dur=8s] ただの怪談扱いするには、条件の話が具体的すぎる。"
        if "诅咒珠" in text and any(marker in text for marker in {"交给", "使用", "拥有"}):
            return "呪いの珠はただの小道具じゃないわ。[EMO preset=thinking dur=8s] 誰が条件を握るかが問題になる。"
        if "满足条件" in text and "诅咒" in text and any(marker in text for marker in {"杀人", "杀死"}):
            return "条件を満たせば殺せる、なんて危険すぎるわ。[EMO preset=serious_speaking dur=8s] 条件そのものが武器になる。"
        if "确凿的证据" in text:
            return "証拠責任に話を戻したわね。[EMO preset=thinking dur=8s] 雑談じゃない、根拠を吐かせる流れよ。"
        if "咒主" in text or "魂渣" in text:
            return "呪主と魂滓は資源システムっぽいわね。[EMO preset=thinking dur=8s] まずはルール経済として記録するべきよ。"
        if "真货" in text or "实际存在" in text:
            return "「本物」って言い方、引っかかるわね。[EMO preset=thinking dur=8s] 噂を検証可能なルールに押し上げている。"
        if any(marker in text for marker in {"能看到", "能够看到", "看得见", "看不见"}):
            return "視認条件が急に重要になったわ。[EMO preset=thinking dur=8s] 見えるかどうかは、普通の感覚の話じゃないかもしれない。"
        if kind == "rule_anomaly":
            return "七大なのに数が合わない？[EMO preset=thinking dur=8s] そういう命名のズレは、たいてい見落としじゃない。"
        if kind == "choice":
            return "急いで選ばないで。[EMO preset=thinking dur=8s] これは前の条件を覚えているか試している流れに見える。"
        if kind == "emotional_beat":
            return "今の反応、少し不自然ね。[EMO preset=serious_speaking dur=8s] ただの感情じゃなく、後で効く信号かもしれない。"
        if kind == "contradiction":
            return "今の言い方、少し引っかかる。[EMO preset=surprised dur=7s] 決定的じゃないけど、丸をつけておく価値はあるわ。"
        if kind == "new_evidence":
            return "この話は覚えておくべきね。[EMO preset=thinking dur=8s] 背景説明に見せて、ルールか動機の端が混じっている。"
        return "情報密度が上がったわね。[EMO preset=thinking dur=8s] 背景音として流すには早い。"
    if not zh:
        return "Hold on.[EMO preset=thinking dur=8s] That line feels more like a clue than filler."
    if "复活秘术" in text and ("实际存在" in text or "相信" in text):
        return "她把“实际存在”和“我相信”绑在一起了。[EMO preset=thinking dur=8s] 这不是证明，更像规则条件的影子。"
    if "复活秘术" in text:
        return "复活秘术先别当成噱头。[EMO preset=thinking dur=8s] 这里该拆的是条件、代价和谁在撒谎。"
    if "诅咒珠" in text and any(marker in text for marker in {"交给", "使用", "拥有"}):
        return "诅咒珠不是普通道具。[EMO preset=thinking dur=8s] 现在争的是谁能控制杀人条件。"
    if "满足条件" in text and "诅咒" in text and any(marker in text for marker in {"杀人", "杀死"}):
        return "“满足条件就能杀人”这点太危险了。[EMO preset=serious_speaking dur=8s] 条件本身就是武器。"
    if "确凿的证据" in text:
        return "他把话题推回证据责任了。[EMO preset=thinking dur=8s] 这不是闲聊，是在逼对方暴露根据。"
    if "咒主" in text or "魂渣" in text:
        return "咒主和魂渣像是资源系统。[EMO preset=thinking dur=8s] 先按规则经济来记。"
    if "真货" in text or "实际存在" in text:
        return "她说的是“真货”。[EMO preset=thinking dur=8s] 这不是介绍怪谈，是把传闻推成可验证的规则。"
    if any(marker in text for marker in {"能看到", "能够看到", "看得见", "看不见"}):
        return "视认条件突然变重要了。[EMO preset=thinking dur=8s] 能不能“看见”可能不是普通感官问题。"
    if kind == "rule_anomaly":
        return "名字叫“七大”，数量却对不上。[EMO preset=thinking dur=8s] 这种命名误差通常不是白给的。"
    if kind == "choice":
        return "先别急着选。[EMO preset=thinking dur=8s] 这里像是在测试我们有没有记住前面的条件。"
    if kind == "emotional_beat":
        return "等一下，这里的情绪波动不太自然。[EMO preset=serious_speaking dur=8s] 我会把它先当成一个信号。"
    if kind == "contradiction":
        return "这句话有点别扭。[EMO preset=surprised dur=7s] 不是决定性矛盾，但值得先圈起来。"
    if kind == "new_evidence":
        return "这个说法先记下来。[EMO preset=thinking dur=8s] 它不像闲聊，更像规则或动机的一块边。"
    return "嗯，信息密度突然上来了。[EMO preset=thinking dur=8s] 先别把它当背景音放过去。"


def _rule_hypothesis_patch(line_event: dict[str, Any], kind: str, score: float) -> dict[str, Any]:
    text = strip_vn_tags(str(line_event.get("text") or "")).strip()
    claim_map = {
        "new_evidence": "当前台词可能包含规则或证据线索。",
        "emotional_beat": "当前台词可能标记了有后续价值的情绪变化。",
        "contradiction": "当前台词可能和此前叙述框架存在不一致。",
        "choice": "当前台词可能引入了需要保留上下文的选择点。",
        "rule_anomaly": "怪谈名称或数量可能存在有意设计的不一致。",
    }
    claim = _claim_from_line(text, kind, line_event, as_hypothesis=True) or claim_map.get(kind, "当前台词值得稍后回看。")
    family = _claim_family(text, kind)
    return {
        "layer": "hypothesis",
        "target": "hypotheses",
        "action": "upsert",
        "item": {
            "id": f"hyp_{family}" if family else f"hyp_{text_hash(str(line_event.get('script_id') or line_event.get('line_id') or 'line'))}_{kind}",
            "claim": claim,
            "confidence": round(max(0.25, min(score / 5, 0.75)), 2),
            "status": "open",
            "tags": [tag for tag in [kind, family] if tag],
            "evidence_line_ids": [line_event.get("line_id")],
            "evidence_script_ids": [line_event.get("script_id")],
            "affect_anchor": kind if kind == "emotional_beat" else "",
            "dramatic_function": kind,
        },
    }


def _rule_evidence_node_patch(line_event: dict[str, Any], attention: dict[str, Any]) -> dict[str, Any]:
    text = strip_vn_tags(str(line_event.get("text") or "")).strip()
    script_id = str(line_event.get("script_id") or "")
    line_id = str(line_event.get("line_id") or "")
    kind = str(attention.get("current_kind") or "evidence")
    claim = _claim_from_line(text, kind, line_event, as_hypothesis=False) or (
        f"当前台词包含 {kind} 线索。" if text else "出现了一条可能和剧情有关的台词。"
    )
    return {
        "layer": "candidate_fact",
        "target": "evidence_nodes",
        "action": "upsert",
        "item": {
            "id": f"ev_{text_hash(script_id or line_id or text)}_{kind}",
            "claim": claim,
            "quote": text,
            "kind": kind,
            "confidence": 0.72,
            "status": "open",
            "tags": [kind, str((attention.get("density") or ""))],
            "evidence_line_ids": [line_id],
            "evidence_script_ids": [script_id],
            "source": "attention_router",
            "router_reasons": list(attention.get("reasons") or [])[:6],
        },
    }


def _claim_from_line(text: str, kind: str, line_event: dict[str, Any], *, as_hypothesis: bool) -> str:
    speaker = str(line_event.get("speaker") or "").strip()
    actor = speaker or "台词"
    prefix = "工作假设：" if as_hypothesis else ""
    if "本所七大不可思议" in text and "真货" in text:
        return prefix + f"{actor}明确说“本所七大不可思议”是“真货”。"
    if "诅咒珠" in text and any(marker in text for marker in {"交给", "使用", "拥有"}):
        return prefix + "台词把“诅咒珠”的持有/使用/交出作为当前冲突核心。"
    if "满足条件" in text and "诅咒" in text and any(marker in text for marker in {"杀人", "杀死"}):
        return prefix + "台词说明“诅咒珠”可在满足条件时用诅咒杀人。"
    if "不留证据" in text and "诅咒" in text:
        return prefix + "台词说明诅咒杀人可能“不留证据”。"
    if "确凿的证据" in text:
        return prefix + "台词把继续对话的条件转向“确凿的证据”。"
    if "咒主" in text and any(marker in text for marker in {"好几个", "几个", "其他", "同时"}):
        return prefix + "台词暗示存在多个拥有诅咒之力的咒主。"
    if "魂渣" in text:
        return prefix + "台词把“魂渣”呈现为和咒主/复活相关的资源。"
    if "黎明之前" in text:
        return prefix + "台词设置了“黎明之前”的行动期限。"
    if "复活秘术" in text and ("实际存在" in text or "存在" in text):
        return prefix + f"{actor}称“复活秘术”实际存在。"
    if "复活秘术" in text:
        return prefix + "“复活秘术”的条件、代价和真实性仍需要继续追踪。"
    if "能看到我的样子" in text:
        return prefix + "台词询问“能看到我的样子吧”，提示可见性本身可能是规则。"
    if "能够看到" in text:
        return prefix + "台词说兴家君也“能够看到”各种东西，提示可见性本身可能是规则。"
    if "看不见" in text:
        return prefix + "台词说怀疑灵异的人可能“看不见”，提示可见性和相信/怀疑相关。"
    if any(marker in text for marker in {"能看到", "看得见"}):
        return prefix + "台词提到“能看到/看得见”，提示可见性本身可能是规则。"
    if "七大不可思议" in text and _has_count_anomaly_text(text):
        return prefix + "“七大不可思议”的名称与约９个故事的数量不一致。"
    if kind == "contradiction":
        return prefix + "当前台词引入了可能需要回查的前后不一致。"
    if kind == "emotional_beat":
        return prefix + "当前台词标记了可能有后续价值的情绪变化。"
    return ""


def _claim_family(text: str, kind: str) -> str:
    if "本所七大不可思议" in text and "真货" in text:
        return "honjo_seven_mysteries_real"
    if "诅咒珠" in text and any(marker in text for marker in {"交给", "使用", "拥有"}):
        return "curse_bead_control"
    if "满足条件" in text and "诅咒" in text and any(marker in text for marker in {"杀人", "杀死"}):
        return "curse_kill_condition"
    if "不留证据" in text and "诅咒" in text:
        return "curse_no_evidence"
    if "确凿的证据" in text:
        return "proof_requirement"
    if "咒主" in text and any(marker in text for marker in {"好几个", "几个", "其他", "同时"}):
        return "multiple_curse_masters"
    if "魂渣" in text:
        return "soul_dregs_resource"
    if "黎明之前" in text:
        return "dawn_deadline"
    if "复活秘术" in text:
        return "revival_secret_technique"
    if any(marker in text for marker in {"能看到", "能够看到", "看得见", "看不见"}):
        return "spirit_visibility_condition"
    if "七大不可思议" in text and _has_count_anomaly_text(text):
        return "seven_mysteries_count_anomaly"
    if any(marker in text for marker in {"刚才", "知道吧", "说过", "说了"}) and any(marker in text for marker in {"但是", "可是", "对了"}):
        return "memory_continuity_cue"
    return "choice_context" if kind == "choice" else ""


def _is_trailing_contrast_setup(text: str) -> bool:
    compact = re.sub(r"[\s。.!！?？…‥]+$", "", str(text or "").strip())
    return compact.endswith(("但是", "可是")) and len(compact) <= 18


def _has_count_anomaly_text(text: str) -> bool:
    value = strip_vn_tags(str(text or ""))
    return any(
        marker in value
        for marker in {
            "９个",
            "9个",
            "九个",
            "９个故事",
            "9个故事",
            "九个故事",
            "七个故事",
            "７个故事",
            "7个故事",
            "九大不可思议",
            "十五大不可思议",
        }
    )


def _rule_character_mention_patches(line_event: dict[str, Any], attention: dict[str, Any]) -> list[dict[str, Any]]:
    text = strip_vn_tags(str(line_event.get("text") or "")).strip()
    names = _extract_character_mentions(text)
    patches: list[dict[str, Any]] = []
    for name in names[:4]:
        char_id = f"char_{text_hash(name)}"
        patches.append(
            {
                "layer": "candidate_fact",
                "target": "characters",
                "action": "upsert",
                "item": {
                    "id": char_id,
                    "claim": name,
                    "names": [name],
                    "facts": [
                        {
                            "claim": f"{name} is mentioned in the displayed line.",
                            "evidence_line_ids": [line_event.get("line_id")],
                            "evidence_script_ids": [line_event.get("script_id")],
                            "status": "supported",
                        }
                    ],
                    "emotional_readings": [
                        {
                            "line_id": line_event.get("line_id"),
                            "script_id": line_event.get("script_id"),
                            "emotion": str(attention.get("current_kind") or ""),
                            "confidence": 0.55,
                            "affect_anchor": text[:80],
                        }
                    ]
                    if attention.get("current_kind") == "emotional_beat"
                    else [],
                    "evidence_refs": [
                        {"line_id": line_event.get("line_id"), "script_id": line_event.get("script_id")}
                    ],
                    "open_questions": [],
                },
            }
        )
    return patches


def _extract_character_mentions(text: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(r"([\u4e00-\u9fff]{1,5})(君|小姐|先生|同学|前辈|后辈)", text):
        name = (match.group(1) + match.group(2)).strip()
        if len(name) >= 2 and name not in names:
            names.append(name)
    # Common title card pattern after tag stripping.
    for match in re.finditer(r"(兴家彰吾|福永叶子|兴家君|叶子小姐)", text):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def _rule_scene_summary_patch(short_memory: list[dict[str, Any]], line_event: dict[str, Any]) -> dict[str, Any]:
    recent = list(short_memory or [])[-12:]
    salient = []
    active_characters = []
    for item in recent:
        text = strip_vn_tags(str(item.get("text") or "")).strip()
        speaker = str(item.get("speaker") or "").strip()
        if speaker and speaker not in active_characters:
            active_characters.append(speaker)
        score, kind = _score_line(text)
        if score >= 1.8 or len(salient) < 3:
            salient.append(
                {
                    "line_id": item.get("line_id"),
                    "script_id": item.get("script_id"),
                    "kind": kind,
                    "text": text[:120],
                }
            )
    summary_bits = [entry["text"] for entry in salient[-5:] if entry.get("text")]
    summary = " / ".join(summary_bits)
    return {
        "layer": "summary",
        "target": "story_summary_log",
        "action": "append",
        "item": {
            "id": f"story_{line_event.get('line_id')}",
            "summary": summary,
            "important_beats": salient[-8:],
            "active_characters": active_characters[-8:],
            "open_questions": [],
            "evidence_refs": [
                {"line_id": line_event.get("line_id"), "script_id": line_event.get("script_id")}
            ],
            "affect_anchor": "",
            "dramatic_function": "rolling_scene_summary",
        },
    }



def _rule_retrospective_bias(context_pack: dict[str, Any], line_count: int, window_lines: int, normalize) -> dict[str, Any]:
    recent_lines = list(context_pack.get("recent_lines") or [])
    recent_reactions = list(context_pack.get("recent_reactions") or [])
    evidence_nodes = list(context_pack.get("evidence_nodes") or [])
    hypotheses = list(context_pack.get("hypotheses") or [])
    chars = (context_pack.get("characters") or {}).get("characters") or []
    text_block = "\n".join(str(item.get("text") or "") for item in recent_lines if isinstance(item, dict))

    repeated_speaks = _repeated_speak_count(recent_reactions)
    quote_like_evidence = [
        str(item.get("claim") or "")[:80]
        for item in evidence_nodes
        if isinstance(item, dict)
        and str(item.get("claim") or "").strip()
        and str(item.get("claim") or "").strip() == str(item.get("quote") or "").strip()
        and len(str(item.get("claim") or "")) >= 18
    ]
    generic_hypotheses = [
        str(item.get("id") or "")
        for item in hypotheses
        if isinstance(item, dict) and str(item.get("claim") or "").startswith("Current line may")
    ]
    bad_names = []
    for item in chars:
        if not isinstance(item, dict):
            continue
        for name in item.get("names") or []:
            value = str(name or "")
            if value.startswith(("我和", "我是", "被", "和")) or len(value) > 8:
                bad_names.append(value)

    boost_topics = _topic_hits(
        text_block,
        ["复活秘术", "本所七大不可思议", "置行堀", "午夜", "叶子", "兴家", "知道", "说过", "但是"],
    )
    watch_for = []
    working_assumptions = []
    plausible_mistakes = []
    emotional_stance = "保持轻微警惕，先把异常当信号而不是结论。"
    uncertainty_style = "用假设口吻；可以怀疑，但不要提前断言真相。"

    if any(topic in boost_topics for topic in ["知道", "说过", "但是"]):
        watch_for.append("角色暗示主角已经知道/说过某事时，优先检查记忆不一致")
        working_assumptions.append("主角的记忆或信息状态可能不稳定，但这仍只是工作假设。")
        plausible_mistakes.append("可能把普通犹豫误读成记忆矛盾。")
    if "复活秘术" in boost_topics:
        watch_for.append("再次出现术式、死亡条件、得到/失去等规则词时提高敏感度")
        working_assumptions.append("复活秘术像规则核心，Kurisu 会自然想拆条件和代价。")
        plausible_mistakes.append("可能过早把术式当成完整规则，而实际仍缺条件。")
    if any(topic in boost_topics for topic in ["本所七大不可思议", "置行堀"]):
        watch_for.append("民俗名词再次出现时，关注它是否从背景知识变成可操作规则")
        working_assumptions.append("民俗传说可能是谜题规则的外壳，而非单纯背景。")
    if "叶子" in boost_topics:
        working_assumptions.append("叶子目前应被视为情绪与动机锚点，不直接视作嫌疑结论。")

    reaction_style = ""
    suppress_kinds = []
    route_immediate = "normal"
    if repeated_speaks >= 2:
        reaction_style = "避免重复泛泛的情绪点评；下一次发声应绑定具体词、规则或矛盾。"
        suppress_kinds.append("emotional_beat")
        route_immediate = "quieter"
    elif quote_like_evidence or generic_hypotheses:
        reaction_style = "更偏分析：把直觉落到具体可检验点上，但保留 Kurisu 的犀利口吻。"
        route_immediate = "more_analytical"

    attention_bias = {
        "boost_kinds": ["evidence", "contradiction"] if quote_like_evidence or generic_hypotheses else [],
        "suppress_kinds": suppress_kinds,
        "boost_topics": boost_topics,
        "suppress_topics": [],
        "watch_for": watch_for,
        "reaction_style": reaction_style,
        "summary_debt": ["summary should describe event-state changes, not paste dialogue"] if _summary_looks_quoted(context_pack) else [],
        "evidence_debt": ["split quote-like evidence into atomic claims"] if quote_like_evidence else [],
        "character_debt": [f"normalize possible bad character names: {', '.join(bad_names[:3])}"] if bad_names else [],
        "reasoning_debt": ["replace generic hypothesis templates with story-specific working assumptions"] if generic_hypotheses else [],
    }
    route_bias = {
        "immediate": route_immediate,
        "summary": "event_segments" if attention_bias["summary_debt"] else "normal",
        "fact_extractor": "split_atomic_facts" if quote_like_evidence else "normal",
        "character_modeler": "normalize_entities" if bad_names else "normal",
        "reasoner": "resolve_debts" if generic_hypotheses or quote_like_evidence else "normal",
    }
    strength = 0.25
    if boost_topics:
        strength += 0.1
    if repeated_speaks >= 2:
        strength += 0.2
    if quote_like_evidence or generic_hypotheses or bad_names:
        strength += 0.2
    raw = {
        "schema_version": "vn.retrospective.v1",
        "window": context_pack.get("window") or {"past_lines": len(recent_lines)},
        "attention_bias": attention_bias,
        "character_orientation": {
            "working_assumptions": working_assumptions[:6],
            "emotional_stance": emotional_stance,
            "uncertainty_style": uncertainty_style,
            "plausible_mistakes": plausible_mistakes[:5],
            "next_reaction_bias": reaction_style or "如果下一段出现新信息，先指出具体可疑点，再给轻微角色化反应。",
            "avoid_sounding_like": ["全知旁白", "证据表朗读", "重复同一句情绪模板"],
        },
        "route_bias": route_bias,
        "strength": min(strength, 0.75),
        "ttl_lines": max(20, min(50, window_lines // 2)),
        "confidence": 0.55,
        "notes": ["rules fallback; no future text inspected"],
    }
    return normalize(raw, line_count, source="rules")


def _repeated_speak_count(reactions: list[dict[str, Any]]) -> int:
    counts: dict[str, int] = {}
    for item in reactions:
        text = str((item or {}).get("speak_text") or "").strip()
        if not text:
            continue
        key = re.sub(r"\[EMO[^\]]*\]", "", text).strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values(), default=0)


def _topic_hits(text: str, topics: list[str]) -> list[str]:
    return [topic for topic in topics if topic and topic in text][:8]


def _summary_looks_quoted(context_pack: dict[str, Any]) -> bool:
    logs = list(context_pack.get("story_summary_log") or [])[-4:]
    if not logs:
        return False
    quoted = 0
    for item in logs:
        summary = str((item or {}).get("summary") or "")
        if " / " in summary or summary.count("……") >= 2:
            quoted += 1
    return quoted >= 2


