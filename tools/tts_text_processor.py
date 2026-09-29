"""
TTS 专用文本处理模块。

文本转换不修改调用方的原文、字幕或对话历史。
英文发音资源按需加载并缓存，转换过程不访问网络。

包含：
  - EMO_PRESETS                       情绪预设字典（标签名 → VTS 动作）
  - convert_english_for_japanese_tts   英文整词 / 缩写 → 片假名
  - correct_pronunciation_for_tts     TTS 专用发音修正入口
"""

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 情绪预设
# ---------------------------------------------------------------------------

EMO_PRESETS: dict = {
    "smile":       {"EXPR": {"Smile.exp3.json": {}}},
    "微笑":         {"EXPR": {"Smile.exp3.json": {}}},
    "happy":       {"EXPR": {"Smile.exp3.json": {}}},
    "thinking":    {"EXPR": {"Thinking.exp3.json": {}}},
    "think":       {"EXPR": {"Thinking.exp3.json": {}}},
    "思考":         {"EXPR": {"Thinking.exp3.json": {}}},
    "angry":       {"EXPR": {"Angry.exp3.json": {}}},
    "生气":         {"EXPR": {"Angry.exp3.json": {}}},
    "annoyed":     {"EXPR": {"Angry.exp3.json": {}}},
    "disappointed": {"EXPR": {"Disappointed.exp3.json": {}}},
    "失望":         {"EXPR": {"Disappointed.exp3.json": {}}},
    "沮丧":         {"EXPR": {"Disappointed.exp3.json": {}}},
    "失落":         {"EXPR": {"Disappointed.exp3.json": {}}},
    "sad":         {"EXPR": {"Disappointed.exp3.json": {}}},
}

# ---------------------------------------------------------------------------
# 英文缩写 → 片假名
# ---------------------------------------------------------------------------

# 单字母读音映射
_LETTER_TO_KATAKANA: dict[str, str] = {
    'A': 'エー',   'B': 'ビー',   'C': 'シー',   'D': 'ディー', 'E': 'イー',
    'F': 'エフ',   'G': 'ジー',   'H': 'エイチ', 'I': 'アイ',   'J': 'ジェイ',
    'K': 'ケー',   'L': 'エル',   'M': 'エム',   'N': 'エヌ',   'O': 'オー',
    'P': 'ピー',   'Q': 'キュー', 'R': 'アール', 'S': 'エス',   'T': 'ティー',
    'U': 'ユー',   'V': 'ブイ',   'W': 'ダブリュー', 'X': 'エックス',
    'Y': 'ワイ',   'Z': 'ゼット',
}

# 既有的约定读音；匹配完整 token，不用逐词追加普通英文的修复个案。
_SPECIAL_CASES: dict[str, str] = {
    # Consensus / distributed systems terms
    'Paxos': 'パクソス', 'PAXOS': 'パクソス', 'paxos': 'パクソス',
    'Raft': 'ラフト', 'RAFT': 'ラフト', 'raft': 'ラフト',
    # 技术缩写
    'AI': 'エーアイ', 'GPT': 'ジーピーティー', 'CPU': 'シーピーユー',
    'GPU': 'ジーピーユー', 'API': 'エーピーアイ', 'URL': 'ユーアールエル',
    'HTTP': 'エイチティーティーピー', 'HTTPS': 'エイチティーティーピーエス',
    'HTML': 'エイチティーエムエル', 'CSS': 'シーエスエス', 'JS': 'ジェイエス',
    'JSON': 'ジェイソン', 'XML': 'エックスエムエル', 'SQL': 'エスキューエル',
    'DB': 'ディービー', 'OS': 'オーエス', 'iOS': 'アイオーエス',
    'SSH': 'エスエスエイチ', 'FTP': 'エフティーピー', 'SMTP': 'エスエムティーピー',
    'DNS': 'ディーエヌエス', 'CDN': 'シーディーエヌ', 'VPN': 'ブイピーエヌ',
    'LAN': 'ラン', 'WAN': 'ワン', 'WiFi': 'ワイファイ', 'USB': 'ユーエスビー',
    'HDMI': 'エイチディーエムアイ', 'SD': 'エスディー', 'SSD': 'エスエスディー',
    'HDD': 'エイチディーディー', 'RAM': 'ラム', 'ROM': 'ロム',
    'BIOS': 'バイオス', 'UEFI': 'ユーイーエフアイ',
    'PDF': 'ピーディーエフ', 'CSV': 'シーエスブイ',
    'MP3': 'エムピースリー', 'MP4': 'エムピーフォー',
    'WAV': 'ウェーブ', 'FLAC': 'フラック', 'AAC': 'エーエーシー',
    'JPG': 'ジェイペグ', 'JPEG': 'ジェイペグ', 'PNG': 'ピーエヌジー',
    'GIF': 'ジフ', 'SVG': 'エスブイジー',
    # ブランド / プラットフォーム
    'AWS': 'エーダブリューエス', 'GCP': 'ジーシーピー',
    'OpenAI': 'オープンエーアイ', 'ChatGPT': 'チャットジーピーティー',
    'Gemini': 'ジェミニ', 'Claude': 'クロード', 'Copilot': 'コパイロット',
    'GitHub': 'ギットハブ', 'Docker': 'ドッカー',
    'Bilibili': 'ビリビリ', 'bilibili': 'ビリビリ', 'BiliBili': 'ビリビリ',
    'BILIBILI': 'ビリビリ',
    'Windows': 'ウィンドウズ', 'Android': 'アンドロイド', 'Linux': 'リナックス',
    'Bluetooth': 'ブルートゥース', 'Google': 'グーグル', 'Apple': 'アップル',
    'Microsoft': 'マイクロソフト', 'Amazon': 'アマゾン', 'Spotify': 'スポティファイ',
    'YouTube': 'ユーチューブ', 'Netflix': 'ネットフリックス',
    'Instagram': 'インスタグラム', 'Twitter': 'ツイッター',
    'TikTok': 'ティックトック', 'Discord': 'ディスコード',
    'Slack': 'スラック', 'Zoom': 'ズーム',
    # Adobe / デザイン
    'Figma': 'フィグマ', 'PS': 'ピーエス', 'AE': 'エーイー',
}

# Match Latin tokens independently of surrounding Japanese word characters.
# Pronunciation overrides apply to whole tokens, never substrings of a word.
_LATIN_TOKEN_PATTERN = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*(?:[0-9]+[A-Za-z]*)?")
_LATIN_WORD_PATTERN = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")


@lru_cache(maxsize=1)
def _japanese_user_dictionary():
    dictionary_dir = Path(__file__).resolve().parents[1] / "GPT_SoVITS/text/ja_userdic"
    if not (dictionary_dir / "userdict.csv").is_file() and not (dictionary_dir / "user.dict").is_file():
        return None
    try:
        # The Japanese frontend owns compilation and registration of this resource.
        from GPT_SoVITS.text.japanese import dictionary_word_reading
    except ModuleNotFoundError as exc:
        if exc.name != "pyopenjtalk":
            raise
        logger.warning("Local Japanese dictionary requires the local-models profile")
        return None
    return dictionary_word_reading


@lru_cache(maxsize=1)
def _english_kana_resources():
    from e2k import C2K, P2K

    # Reuse the pronunciation lexicon already shipped with the English frontend.
    dictionary_path = Path(__file__).resolve().parents[1] / "GPT_SoVITS/text/cmudict-fast.rep"
    pronunciations = {}
    with dictionary_path.open(encoding="utf-8") as source:
        for line in source:
            fields = line.split()
            if fields:
                word = fields[0].split("(", 1)[0].lower()
                pronunciations.setdefault(word, fields[1:])
    return pronunciations, P2K(max_len=64), C2K(max_len=64)


@lru_cache(maxsize=4096)
def _english_word_to_katakana(word: str) -> str:
    dictionary_reading = _japanese_user_dictionary()
    if dictionary_reading is not None:
        reading = dictionary_reading(word)
        if reading is not None:
            return reading
    pronunciations, phonemes_to_kana, characters_to_kana = _english_kana_resources()
    phones = pronunciations.get(word)
    return phonemes_to_kana(phones) if phones else characters_to_kana(word)


def convert_english_for_japanese_tts(text: str) -> str:
    """Read ordinary words as Japanese loanwords and uppercase initials by letter name."""
    def _read_word(match: re.Match) -> str:
        word = match.group(0)
        if word in _SPECIAL_CASES:
            return _SPECIAL_CASES[word]
        if word.isupper():
            return "".join(_LETTER_TO_KATAKANA.get(ch, ch) for ch in word)
        return _english_word_to_katakana(word.replace("’", "'").lower())

    def _convert(match: re.Match) -> str:
        token = match.group(0)
        return _SPECIAL_CASES.get(token) or _LATIN_WORD_PATTERN.sub(_read_word, token)

    return _LATIN_TOKEN_PATTERN.sub(_convert, text)


def correct_pronunciation_for_tts(text: str, output_language: str = "ja") -> str:
    """
    TTS 发音修正入口。

    当前修正内容：
      - 专有名词读音（如「牧瀬紅莉栖」→「牧瀬クリス」）
      - 日文输出中的英文整词 / 缩写 → 片假名

    普通英文优先使用本机日文用户词典的整词读音；未完整收录的词
    通过通用的发音 / 片假名转换处理。
    """
    replacements = {
        "牧瀬紅莉栖": "牧瀬クリス",
        "牧濑红莉栖": "牧瀬クリス",
        "牧瀬红莉栖": "牧瀬クリス",
        "牧濑紅莉栖": "牧瀬クリス",
        "紅莉栖": "クリス",
        "红莉栖": "クリス",
        "哔哩哔哩": "ビリビリ",
    }
    for original, corrected in replacements.items():
        text = text.replace(original, corrected)

    if str(output_language).strip().lower() in {"ja", "jp", "japanese", "日文"}:
        text = convert_english_for_japanese_tts(text)
    return text
