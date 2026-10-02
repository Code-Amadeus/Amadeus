# -*- coding: utf-8 -*-
"""Prebuild exact-match audio cache for likely Kurisu first utterances.

This does not bypass the LLM.  It only pre-synthesizes audio for short first
sentences that the LLM may actually emit later.  At runtime, the real LLM first
sentence is still generated first; then the audio cache is checked by exact text
and TTS parameters.

Dry run:
    python tools/prebuild_first_sentence_audio_cache.py --limit 20

Build cache:
    python tools/prebuild_first_sentence_audio_cache.py --run --limit 1000
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config.settings import (  # noqa: E402
    FIRST_SENTENCE_AUDIO_CACHE_MAX_SECONDS,
    TTS_GPT_MODEL_PATH,
    TTS_SOVITS_MODEL_PATH,
)
from tools.tts_text_processor import correct_pronunciation_for_tts  # noqa: E402
from tts.first_sentence_audio_cache import get_first_sentence_audio_cache  # noqa: E402
from tts.pipeline import (  # noqa: E402
    _get_ref_audio,
    _get_ref_text,
    _strip_tts_fullwidth_parentheses,
    get_sovits_params,
)


KURISU_FIRST_UTTERANCES = [
    "そうね。",
    "いい質問ね。",
    "説明するわ。",
    "結論から言うわ。",
    "まず要点から話すわ。",
    "順番に見ていくわ。",
    "そこが重要ね。",
    "少し待ちなさい。",
    "考えてみましょう。",
    "それは基本ね。",
    "悪くない質問ね。",
    "ふむ、なるほど。",
    "なるほどね。",
    "そこから始めましょう。",
    "簡単に言うわ。",
    "正確には違うわ。",
    "少し補足するわ。",
    "そこは誤解しやすいわ。",
    "まず前提からね。",
    "いいわ、説明する。",
    "論点を整理するわ。",
    "手短に答えるわ。",
    "ちゃんと聞きなさい。",
    "焦らなくていいわ。",
    "落ち着いて考えましょう。",
    "その見方は近いわ。",
    "少し違うわね。",
    "本質はそこじゃないわ。",
    "重要なのはここよ。",
    "それなら説明できるわ。",
    "まず定義からね。",
    "例で考えましょう。",
    "直感的に言うわ。",
    "技術的にはね。",
    "そこは面白い点ね。",
    "良い観察ね。",
    "悪くない着眼点ね。",
    "その疑問は自然ね。",
    "じゃあ整理するわ。",
    "では始めましょう。",
    "少し細かく見るわ。",
    "大枠から話すわ。",
    "必要な部分だけ言うわ。",
    "そこは慎重に見て。",
    "まず確認するわ。",
    "一つずつ行くわ。",
    "ここは大事よ。",
    "理解は近いわ。",
    "まだ足りないわね。",
    "もう少し厳密に言うわ。",
    "それは良い問いね。",
    "かなり核心に近いわ。",
    "順序立てて説明するわ。",
    "先に結論を言うわ。",
    "先に答えるわ。",
    "短く言うとね。",
    "単純化するとね。",
    "厳密にはこうよ。",
    "まず状況を分けるわ。",
    "比較して考えましょう。",
    "そこは区別して。",
    "その前提ならね。",
    "条件次第ね。",
    "一般にはそうね。",
    "例外はあるわ。",
    "そこを見落とさないで。",
    "論理的に見ましょう。",
    "少し数学的に言うわ。",
    "実装目線で言うわ。",
    "設計としてはね。",
    "たぶんこうね。",
    "私ならこう考えるわ。",
    "紅莉栖的にはね。",
    "助手扱いはやめなさい。",
    "まったく、仕方ないわね。",
    "それくらい分かるでしょ。",
    "でも説明してあげる。",
    "特別に教えてあげるわ。",
    "感謝しなさいよね。",
    "別に難しくないわ。",
    "難しく見えるだけよ。",
    "ここで混乱しやすいの。",
    "まず名前に惑わされないで。",
    "直感を作るのが先ね。",
    "細部は後でいいわ。",
    "最初は単純化するわ。",
    "全体像からね。",
    "関係を整理するわ。",
    "因果を分けて考えて。",
    "目的から見るべきね。",
    "制約を確認するわ。",
    "失敗条件から見ましょう。",
    "安全側に考えるわ。",
    "実験して確かめましょう。",
    "ログを見れば分かるわ。",
    "まず症状を切り分けるわ。",
    "原因候補を並べるわ。",
    "そこは再現性が重要ね。",
    "観測から始めるわ。",
    "推測だけでは危険ね。",
    "証拠を見ましょう。",
    "そこは後で詰めるわ。",
    "今は方針を決めましょう。",
    "実用上は十分ね。",
    "理論上は可能よ。",
    "その設計はありね。",
    "少し重い設計ね。",
    "もっと軽くできるわ。",
    "それは筋がいいわ。",
    "方向性は正しいわ。",
    "ただし注意点があるわ。",
    "先に注意しておくわ。",
    "前提を置くわね。",
    "仮定を明確にするわ。",
    "ここでは単純化するわ。",
    "まず答えだけ言うわ。",
    "詳しくはこうね。",
    "考え方はこうよ。",
    "流れは単純よ。",
    "見れば分かるわ。",
    "聞き逃さないで。",
    "そこ、重要よ。",
    "よく見て。",
    "落ち着きなさい。",
    "問題ないわ。",
    "大丈夫、説明するわ。",
    "では本題に入るわ。",
    "話を戻すわ。",
    "ここからが本題ね。",
    "まず一言で言うわ。",
    "答えは単純よ。",
    "理由は二つあるわ。",
    "ポイントは一つね。",
    "核心はここよ。",
    "そこを押さえて。",
    "これで分かるはずよ。",
    "いい？説明するわ。",
    "理解したいなら聞いて。",
    "まず図式化するわ。",
    "モデル化するとね。",
    "抽象化するとね。",
    "実際にはこうよ。",
    "表面的にはそうね。",
    "内部では違うわ。",
    "見かけに騙されないで。",
    "そこは罠ね。",
    "一度切り分けるわ。",
    "順番が逆ね。",
    "まず入力を見ましょう。",
    "出力から確認するわ。",
    "タイミングが重要ね。",
    "遅延を分けて考えるわ。",
    "キャッシュの話ね。",
    "音声側の問題ね。",
    "レンダリング側とは別ね。",
    "その可能性はあるわ。",
    "かなりあり得るわ。",
    "それは薄いわね。",
    "検証してみましょう。",
    "測れば分かるわ。",
]

_TARGET_FIRST_UTTERANCE_COUNT = 1000


def _unique_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _build_expanded_first_utterances() -> list[str]:
    texts = _unique_texts(KURISU_FIRST_UTTERANCES)
    topics = [
        "Paxos",
        "Raft",
        "Multi-Paxos",
        "分散合意",
        "合意アルゴリズム",
        "リーダー選出",
        "ログ複製",
        "一貫性",
        "可用性",
        "CAP定理",
        "TTS",
        "ASR",
        "AEC",
        "VAD",
        "音声認識",
        "音声合成",
        "割り込み",
        "バージイン",
        "エコーキャンセル",
        "マイク入力",
        "SenseVoice",
        "Qwen",
        "LLM",
        "プロバイダ",
        "ストリーミング",
        "KVキャッシュ",
        "CUDA Graph",
        "FlashAttention",
        "BigVGAN",
        "SoVITS",
        "GPT-SoVITS",
        "首声",
        "初回遅延",
        "キャッシュ",
        "推論速度",
        "GPUメモリ",
        "PyTorch",
        "Electron",
        "PyQt",
        "SpriteForge",
        "Pixi",
        "レンダリング",
        "壁紙モード",
        "表情制御",
        "口型同期",
        "字幕",
        "状態機械",
        "runtime",
        "graph",
        "AUIP",
        "manifest",
        "schema",
        "callback",
        "checkpoint",
        "provider",
        "OpenClaw",
        "コード生成",
        "五目並べ",
        "UI",
        "GUI",
        "セッション",
        "履歴",
        "会話履歴",
        "割り込み履歴",
        "ローカルLLM",
        "リモートLLM",
        "Bedrock",
        "DeepSeek",
        "Claude",
        "翻訳",
        "発音補正",
        "句読点",
        "早切り",
        "T2S",
        "静的KV",
        "CUDA",
        "cu124",
        "Python",
        "Windows",
        "WASAPI",
        "DirectSound",
        "Bluetooth",
        "内蔵マイク",
        "ノイズ",
        "音量",
        "レイテンシ",
        "ベンチマーク",
        "デバッグ",
        "ログ",
        "ファイル管理",
        "キャッシュ管理",
        "環境分離",
        "venv",
        "依存関係",
        "メモリ",
        "動画化",
        "画像圧縮",
        "KTX2",
        "テクスチャ",
        "プリロード",
        "GPUアップロード",
        "デスクトップモード",
        "音声ウェイク",
        "ウェイクワード",
        "テンプレート照合",
    ]
    topic_templates = [
        "{topic}の話ね。",
        "{topic}についてね。",
        "{topic}を整理するわ。",
        "{topic}なら説明するわ。",
        "{topic}なら要点から話すわ。",
        "{topic}は基本から見るわ。",
        "{topic}は順番に話すわ。",
        "{topic}の核心から言うわ。",
        "{topic}なら任せなさい。",
        "{topic}を短く説明するわ。",
        "{topic}の前提からね。",
        "{topic}を確認するわ。",
    ]
    connector_templates = [
        "{topic}ね、聞きなさい。",
        "{topic}ね、いいわ。",
        "{topic}ね、説明するわ。",
        "{topic}ね、少し待って。",
    ]

    generated: list[str] = []
    for template in topic_templates:
        generated.extend(template.format(topic=topic) for topic in topics)
    for template in connector_templates:
        generated.extend(template.format(topic=topic) for topic in topics)

    texts = _unique_texts(texts + generated)
    return texts[:_TARGET_FIRST_UTTERANCE_COUNT]


KURISU_FIRST_UTTERANCES = _build_expanded_first_utterances()


@dataclass
class CacheItem:
    raw_text: str
    processed_text: str
    params: dict
    cache_path: Path
    cached: bool


def _prepare_item(text: str) -> CacheItem:
    tts_text = _strip_tts_fullwidth_parentheses(text.strip())
    params = get_sovits_params(tts_text, is_first_sentence=True)
    params["ref_audio_path"] = _get_ref_audio(tts_text)
    params["prompt_text"] = _get_ref_text(tts_text)
    params["chunk_size_seconds"] = None
    processed_text = correct_pronunciation_for_tts(tts_text)

    cache = get_first_sentence_audio_cache()
    cache_path = cache.path_for(processed_text, params)
    cached = cache.lookup(processed_text, params) is not None
    return CacheItem(
        raw_text=text,
        processed_text=processed_text,
        params=params,
        cache_path=cache_path,
        cached=cached,
    )


def _iter_items(limit: int | None, shuffle: bool) -> list[CacheItem]:
    texts = list(dict.fromkeys(KURISU_FIRST_UTTERANCES))
    if shuffle:
        random.shuffle(texts)
    if limit is not None:
        texts = texts[:limit]
    return [_prepare_item(text) for text in texts]


def _load_inferencer():
    from local_tts_infer import TTSInferencer

    inferencer = TTSInferencer(
        sovits_path=TTS_SOVITS_MODEL_PATH,
        gpt_path=TTS_GPT_MODEL_PATH,
    )
    inferencer._stream_sync_timing_enabled = False
    return inferencer


def _synthesize_to_cache(inferencer, item: CacheItem, force: bool) -> tuple[str, float, Path]:
    if item.cached and not force:
        return "hit", 0.0, item.cache_path

    chunks = []
    sr = None
    t0 = time.perf_counter()
    for result in inferencer.infer_stream(text=item.processed_text, **item.params):
        if len(result) == 3:
            sr_item, audio_chunk, _text_item = result
        else:
            sr_item, audio_chunk = result
        if audio_chunk is None or len(audio_chunk) == 0:
            continue
        sr = int(sr_item)
        chunks.append(np.asarray(audio_chunk, dtype=np.float32))

    elapsed = time.perf_counter() - t0
    if not chunks or sr is None:
        return "empty", elapsed, item.cache_path

    audio = np.concatenate(chunks).astype(np.float32, copy=False)
    duration = float(audio.size) / float(sr)
    stored = get_first_sentence_audio_cache().store(
        item.processed_text,
        item.params,
        sr,
        audio,
        raw_text=item.raw_text,
        source="prebuild",
    )
    if stored:
        return f"stored {duration:.2f}s", elapsed, item.cache_path
    return f"skip {duration:.2f}s>{FIRST_SENTENCE_AUDIO_CACHE_MAX_SECONDS:.2f}s", elapsed, item.cache_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="actually synthesize and write cache files")
    parser.add_argument("--limit", type=int, default=None, help="maximum number of phrases to process")
    parser.add_argument("--force", action="store_true", help="rebuild even if the cache already exists")
    parser.add_argument("--shuffle", action="store_true", help="shuffle phrase order before limiting")
    parser.add_argument("--list", action="store_true", help="print all candidate phrases")
    args = parser.parse_args()

    if args.list:
        for index, text in enumerate(KURISU_FIRST_UTTERANCES, start=1):
            print(f"{index:03d} {text}")
        return 0

    items = _iter_items(args.limit, args.shuffle)
    cached_count = sum(1 for item in items if item.cached)
    print(f"phrases={len(items)} cached={cached_count} max_cache_sec={FIRST_SENTENCE_AUDIO_CACHE_MAX_SECONDS:.2f}")
    print(f"gpt={TTS_GPT_MODEL_PATH}")
    print(f"sovits={TTS_SOVITS_MODEL_PATH}")

    if not args.run:
        for index, item in enumerate(items, start=1):
            status = "hit" if item.cached else "miss"
            print(f"{index:03d} {status:4s} {item.raw_text} -> {item.cache_path.name}")
        print("dry-run only; add --run to synthesize audio")
        return 0

    inferencer = _load_inferencer()
    stats = {"hit": 0, "stored": 0, "skip": 0, "empty": 0}
    for index, item in enumerate(items, start=1):
        status, elapsed, path = _synthesize_to_cache(inferencer, item, args.force)
        key = status.split()[0]
        stats[key] = stats.get(key, 0) + 1
        print(f"{index:03d}/{len(items):03d} {status:14s} {elapsed:6.2f}s {item.raw_text} -> {path.name}")

    print(f"done stats={stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
