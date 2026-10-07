"""Host-owned Japanese line contracts; role files provide optional wording overrides.

Facts are substituted once. This catalog never reads a role pack or current state.
"""
from dataclasses import dataclass
from string import Template
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class VoiceLineSpec:
    template: str
    facts: frozenset[str] = frozenset()


VOICE_LINES = MappingProxyType({
    'auip_fact_receipt_pending': VoiceLineSpec('直近の操作はアプリの確認待ちで、まだ反映済みとは言えません。'),
    'auip_fact_receipt_rejected_reason': VoiceLineSpec('直近の操作はアプリに受理されませんでした。理由は「${detail}」です。', frozenset(['detail'])),
    'auip_fact_receipt_rejected': VoiceLineSpec('直近の操作はアプリに受理されませんでした。'),
    'auip_fact_receipt_missing': VoiceLineSpec('直近の操作にはアプリから結果が返っていないため、反映されたかは確認できません。'),
    'auip_fact_receipt_accepted': VoiceLineSpec('キャラクター側の直近の操作はアプリに受理され、状態更新 ${revision} まで反映済みです。', frozenset(['revision'])),
    'auip_fact_no_accepted_action': VoiceLineSpec('このセッションでは、キャラクター側の操作が受理された記録はまだありません。'),
    'auip_fact_controller_execution_current': VoiceLineSpec('アプリから、現在の方針がHost発行のController lease中に少なくとも一度は実行されたという検証済みの報告があります。個々の実行イベントは現在値や累計値ではないため、その payload を局面全体の数としては扱いません。'),
    'auip_fact_controller_execution_earlier': VoiceLineSpec('このセッションでは、以前のController方針が少なくとも一度は実行されたという検証済みの報告があります。現在の方針についての実行証明ではありません。個々の実行イベントは現在値や累計値ではないため、その payload を局面全体の数としては扱いません。'),
    'auip_fact_latest_event': VoiceLineSpec('Hostが受理した直近の重要なアプリイベントは ${event} です。これは一つの結果イベントで、現在の局面は続く state の事実を優先します。', frozenset(['event'])),
    'auip_fact_controller_authority_active': VoiceLineSpec('Hostが発行した操作権限は現在も有効です。'),
    'auip_fact_controller_authority_stopping': VoiceLineSpec('現在は操作権限を取り消して、安全な停止完了を待っています。'),
    'auip_fact_controller_idle_verified': VoiceLineSpec('現在は操作していませんが、それは過去の確認済み実行結果を取り消すものではありません。'),
    'auip_fact_controller_idle_unverified': VoiceLineSpec('現在は操作していません。過去に実行したかどうかは、この現在状態だけでは判断できません。'),
    'auip_fact_sequence_complete': VoiceLineSpec('全 ${count} 段階が完了しています。', frozenset(['count'])),
    'auip_fact_sequence_progress': VoiceLineSpec('全 ${count} 段階中 ${completed} 段階まで完了していて、次は「${next_step}」です。', frozenset(['completed', 'count', 'next_step'])),
    'auip_fact_grid': VoiceLineSpec('盤面は ${width}×${height} で、埋まっているマスは ${occupied} 個です。', frozenset(['height', 'occupied', 'width'])),
    'auip_fact_metrics': VoiceLineSpec('${metrics}です。', frozenset(['metrics'])),
    'auip_fact_controller_policy': VoiceLineSpec('アプリ内Controllerは「${policy}」という方針で稼働中です。', frozenset(['policy'])),
    'auip_fact_controller_safe_stop': VoiceLineSpec('アプリ内Controllerは安全な引き継ぎ地点で停止中です。'),
    'auip_fact_controller_blocked': VoiceLineSpec('アプリ内Controllerは「${reason}」のため停止しています。', frozenset(['reason'])),
    'auip_fact_action_candidates': VoiceLineSpec('アプリがキャラクター側向けに示している操作候補は「${options}」です。', frozenset(['options'])),
    'auip_fact_no_action_candidates': VoiceLineSpec('アプリがキャラクター側向けに示している操作候補は今はありません。'),
    'auip_fact_user_controls_distinct': VoiceLineSpec('これはユーザーの画面操作全体の一覧ではありません。'),
    'auip_fact_participant_owner': VoiceLineSpec('キャラクター側'),
    'auip_fact_turn_owner': VoiceLineSpec('現在の手番は${owner}です。', frozenset(['owner'])),
    'auip_fact_connected_revision': VoiceLineSpec('アプリは接続中で、現在の状態更新は ${revision} です。', frozenset(['revision'])),
    'auip_fact_custom_state': VoiceLineSpec('${label} は ${value} です。', frozenset(['label', 'value'])),
    'auip_fact_selected_state': VoiceLineSpec('現在の ${label} は ${value} です。', frozenset(['label', 'value'])),
    'auip_fact_capabilities': VoiceLineSpec('このアプリでは、キャラクター側は${modes}ができます。', frozenset(['modes'])),
    'auip_fact_no_capabilities': VoiceLineSpec('このアプリでは、キャラクター側が参加できる方法は公開されていません。'),
    'task_voice_queued': VoiceLineSpec('まだ実行待ちです。今のところ問題はありません。次の対応は「${next_action}」です。', frozenset(['next_action'])),
    'task_voice_direction_blocked': VoiceLineSpec('まだ作業中です。進め方は更新されていますが、問題は「${blocker}」です。次の対応は「${next_action}」です。', frozenset(['blocker', 'next_action'])),
    'task_voice_direction_running': VoiceLineSpec('まだ作業中です。進め方は更新されていて、内容は画面に表示しています。まだ完了報告ではありませんが、今のところ問題はありません。'),
    'task_voice_reported_blocked': VoiceLineSpec('まだ作業中です。実行側からは「${recent_result}」と報告されていますが、まだホスト確認済みの結果ではありません。問題は「${blocker}」です。次の対応は「${next_action}」です。', frozenset(['blocker', 'next_action', 'recent_result'])),
    'task_voice_reported_running': VoiceLineSpec('まだ作業中です。実行側からは「${recent_result}」と報告されていますが、まだホスト確認済みの結果ではありません。今のところ問題はありません。', frozenset(['recent_result'])),
    'task_voice_result_blocked': VoiceLineSpec('まだ作業中です。「${recent_result}」。ただし、問題は「${blocker}」です。次の対応は「${next_action}」です。', frozenset(['blocker', 'next_action', 'recent_result'])),
    'task_voice_result_running': VoiceLineSpec('まだ作業中です。「${recent_result}」。今のところ問題はありません。次の対応は「${next_action}」です。', frozenset(['next_action', 'recent_result'])),
    'task_voice_running_no_milestone': VoiceLineSpec('「${title}」を進めています。確認できる新しい成果も問題も、今のところ出ていません。次の対応は「${next_action}」です。検証結果が出たら知らせます。', frozenset(['next_action', 'title'])),
    'task_voice_stalled': VoiceLineSpec('今はここで止まっています。${recent_clause}問題は「${blocker}」です。次の対応は「${next_action}」です。', frozenset(['blocker', 'next_action', 'recent_clause'])),
    'task_voice_terminal': VoiceLineSpec('この作業は終了しています。確認できた結果は「${recent_result}」です。次の対応は「${next_action}」です。', frozenset(['next_action', 'recent_result'])),
    'task_voice_stage': VoiceLineSpec('今は「${stage}」です。「${recent_result}」。次の対応は「${next_action}」です。', frozenset(['next_action', 'recent_result', 'stage'])),
    'task_fact_reported_clause': VoiceLineSpec('実行側からは「${recent_result}」と報告されていますが、まだホスト確認済みではありません。', frozenset(['recent_result'])),
    'task_fact_verified_clause': VoiceLineSpec('ここまでに「${recent_result}」は確認できています。', frozenset(['recent_result'])),
    'task_fact_design': VoiceLineSpec('実装方針は固まっています'),
    'task_fact_diagnostic': VoiceLineSpec('対処すべき具体的な原因まで絞り込めています'),
    'task_fact_capability': VoiceLineSpec('主要な機能の実装まで進んでいます'),
    'task_fact_validation': VoiceLineSpec('新しい検証結果まで確認できています'),
    'task_fact_direction': VoiceLineSpec('現在の進め方は更新されていて、その方針で進めています'),
    'task_fact_terminal_result': VoiceLineSpec('最終結果は台帳に記録済みで、詳細は画面に表示しています'),
    'task_fact_no_result': VoiceLineSpec('確認できる新しい意味的成果はまだありません'),
    'browser_voice_identity_gate': VoiceLineSpec('ページが本人確認を求めているため、依頼された結果にはまだ到達していません。'),
    'browser_voice_page_missing': VoiceLineSpec('指定されたページは存在しないため、依頼された内容には到達できませんでした。'),
    'browser_voice_access_error': VoiceLineSpec('ページ側でエラーまたはアクセス制限が発生し、依頼された結果には到達できませんでした。'),
    'browser_voice_failed_page': VoiceLineSpec('操作は完了しませんでした。現在のページは${label}です。', frozenset(['label'])),
    'browser_voice_failed': VoiceLineSpec('操作は完了しませんでした。'),
    'browser_voice_input_page': VoiceLineSpec('続けるには追加の情報が必要です。現在のページは${label}です。', frozenset(['label'])),
    'browser_voice_input': VoiceLineSpec('続けるには追加の情報が必要です。'),
    'browser_voice_conflict_page': VoiceLineSpec('操作結果の報告と現在のページが一致していません。現在のページは${label}です。確認が必要です。', frozenset(['label'])),
    'browser_voice_conflict': VoiceLineSpec('操作結果の報告を確認できませんでした。確認が必要です。'),
    'browser_voice_verified_page': VoiceLineSpec('操作は完了しました。現在のページは${label}です。', frozenset(['label'])),
    'browser_voice_verified': VoiceLineSpec('操作は完了しました。'),
    'browser_voice_unverified_page': VoiceLineSpec('操作は終了しました。現在のページは${label}です。報告の内容はまだ確認が必要です。', frozenset(['label'])),
    'browser_voice_unverified': VoiceLineSpec('操作は終了しましたが、結果の内容はまだ確認が必要です。'),
    'outcome_voice_verified': VoiceLineSpec('外部操作は完了し、結果も確認できました。'),
    'outcome_voice_unverified': VoiceLineSpec('外部操作は終了しましたが、結果は確認できませんでした。'),
    'outcome_voice_failed': VoiceLineSpec('外部操作は完了しませんでした。'),
    'outcome_voice_auip_verified': VoiceLineSpec('AUIPアプリとして検証できました。起動結果はHostの接続確認で確定します。'),
    'outcome_voice_auip_mode_missing': VoiceLineSpec('アプリは生成できましたが、依頼された参加方法にはまだ対応できていません。'),
    'outcome_voice_auip_missing': VoiceLineSpec('AUIP対応のアプリを確認できなかったため、まだ起動できません。'),
    'work_voice_closed': VoiceLineSpec('これで今回の作業は終了です。'),
    'work_voice_monitor_duration': VoiceLineSpec('処理は続いていますが、確認できる新しい節目は${duration}届いていません。', frozenset(['duration'])),
    'work_voice_monitor': VoiceLineSpec('まだ処理は続いています。今のところ、確認できる新しい節目は届いていません。'),
    'work_voice_terminal_excerpt': VoiceLineSpec('作業は完了しました。最終報告では「${excerpt}」という結果になっています。詳しい内容はカードに残しています。', frozenset(['excerpt'])),
    'work_voice_terminal': VoiceLineSpec('こちらで確認しました。この作業は終了しています。'),
    'work_voice_terminal_summary': VoiceLineSpec('こちらで確認しました。この作業は終了しました。概要は「${summary}」です。詳しい根拠はカードに残しています。', frozenset(['summary'])),
    'auip_voice_terminal_title': VoiceLineSpec('${title}は終了しました。結果は画面で確認できます。', frozenset(['title'])),
    'auip_voice_terminal': VoiceLineSpec('終了しました。結果は画面で確認できます。'),
    'auip_voice_operator_unconfirmed': VoiceLineSpec('キャラクター側の操作は確認されませんでした。今回は完了したとは言えません。'),
    'auip_voice_controller_effect': VoiceLineSpec('制御は実際に動いています。詳しい状況は画面で確認できます。'),
    'auip_voice_important_event': VoiceLineSpec('大きな変化が確定しました。結果は画面で確認できます。'),
    'verification_voice_failed': VoiceLineSpec('操作は完了しませんでした。'),
    'verification_voice_unavailable': VoiceLineSpec('操作は終了しましたが、結果を検証する方法がまだありません。'),
    'verification_voice_unconfirmed': VoiceLineSpec('操作は終了しましたが、結果は検証できませんでした。'),
    'auip_fact_launch_capability': VoiceLineSpec('確認済みの対話型アプリでは観戦・共同参加・委任参加ができます。候補が none でも、それは現在この会話で起動可能なアプリを確認できていないという意味です。キャラクター側の恒久的な能力否定に言い換えません。'),
    'auip_voice_launch_missing': VoiceLineSpec('AUIP対応のアプリを確認できなかったため、ゲームは開いていません。'),
    'auip_voice_launch_failed': VoiceLineSpec('アプリを開けなかったため、ゲームはまだ開始していません。'),
    'browser_voice_target_unknown': VoiceLineSpec('今のページは確認できましたが、具体的な操作対象を特定できませんでした。'),
    'browser_voice_action_error': VoiceLineSpec('ブラウザ操作で停止しました: ${detail}', frozenset(['detail'])),
    'browser_voice_instruction_needed': VoiceLineSpec('今のページは確認できましたが、操作対象を特定できませんでした。もう少し具体的に指示してください。'),
    'browser_voice_search_result': VoiceLineSpec('${query} で検索しました。結果ページを確認できる状態にしています。', frozenset(['query'])),
    'vn_voice_resurrection_once': VoiceLineSpec('少し待ってください。[EMO preset=thinking dur=8s] 復活を一回だけ使えるという前提が出ています。条件と代償を先に確認するべきです。'),
    'vn_voice_resurrection': VoiceLineSpec('復活の秘術です。[EMO preset=thinking dur=8s] ただの怪談扱いするには、条件の話が具体的すぎます。'),
    'vn_voice_curse_orb': VoiceLineSpec('呪いの珠はただの小道具ではありません。[EMO preset=thinking dur=8s] 誰が条件を握るかが問題になります。'),
    'vn_voice_lethal_condition': VoiceLineSpec('条件を満たせば殺せるというのは危険すぎます。[EMO preset=serious_speaking dur=8s] 条件そのものが武器になります。'),
    'vn_voice_evidence': VoiceLineSpec('証拠責任に話を戻しました。[EMO preset=thinking dur=8s] 雑談ではなく、根拠を示させる流れです。'),
    'vn_voice_resource_rules': VoiceLineSpec('呪主と魂滓は資源システムに見えます。[EMO preset=thinking dur=8s] まずはルール経済として記録するべきです。'),
    'vn_voice_real_claim': VoiceLineSpec('「本物」という言い方が気になります。[EMO preset=thinking dur=8s] 噂を検証可能なルールに押し上げています。'),
    'vn_voice_visibility': VoiceLineSpec('視認条件が急に重要になりました。[EMO preset=thinking dur=8s] 見えるかどうかは、普通の感覚の話ではないかもしれません。'),
    'vn_voice_rule_anomaly': VoiceLineSpec('七大なのに数が合いません。[EMO preset=thinking dur=8s] そういう命名のズレは、たいてい見落としではありません。'),
    'vn_voice_choice': VoiceLineSpec('急いで選ばないでください。[EMO preset=thinking dur=8s] これは前の条件を覚えているか試している流れに見えます。'),
    'vn_voice_emotion': VoiceLineSpec('今の反応は少し不自然です。[EMO preset=serious_speaking dur=8s] ただの感情ではなく、後で効く信号かもしれません。'),
    'vn_voice_contradiction': VoiceLineSpec('今の言い方が少し気になります。[EMO preset=surprised dur=7s] 決定的ではありませんが、丸をつけておく価値はあります。'),
    'vn_voice_new_evidence': VoiceLineSpec('この話は覚えておくべきです。[EMO preset=thinking dur=8s] 背景説明に見せて、ルールか動機の端が混じっています。'),
    'vn_voice_density': VoiceLineSpec('情報密度が上がりました。[EMO preset=thinking dur=8s] 背景音として流すには早いです。'),
    'focus_voice_drafts': VoiceLineSpec('この会話の Draft に戻しました。次の指定なしの作業は、ここに残ります。'),
    'focus_voice_failed': VoiceLineSpec('プロジェクトの切り替えは失敗しました。元の作業先はそのままにしています。'),
    'focus_voice_project': VoiceLineSpec('「${project_name}」プロジェクトへの切り替えを確認しました。次の作業はここから続けます。', frozenset(['project_name'])),
})


def validate_voice_lines(overrides: object, *, max_chars: int = 8192) -> Mapping[str, str]:
    """Reject unknown keys and templates that drop or invent declared facts."""
    if not isinstance(overrides, Mapping) or not overrides.keys() <= VOICE_LINES.keys():
        raise ValueError("voice_lines must contain only declared semantic keys")
    resolved = {}
    for key, spec in VOICE_LINES.items():
        value = overrides.get(key, spec.template)
        if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > max_chars:
            raise ValueError(f"invalid voice line: {key}")
        value.encode("utf-8", errors="strict")
        template = Template(value)
        if not template.is_valid() or frozenset(template.get_identifiers()) != spec.facts:
            raise ValueError(f"voice line facts do not match the Host contract: {key}")
        resolved[key] = value
    return MappingProxyType(resolved)


def voice_line(key: str, *, language: str = "ja", character_id: str | None = None, **facts: object) -> str:
    """Render the pinned role's wording without interpreting inserted facts."""
    if language != "ja":
        raise ValueError("voice_line currently declares Japanese host lines only")
    spec = VOICE_LINES[key]
    if frozenset(facts) != spec.facts:
        raise ValueError(f"voice line requires exactly these facts: {key}: {sorted(spec.facts)}")
    from llm.character_prompts import _character
    return Template(_character(character_id).voice_lines[key]).substitute(facts)
