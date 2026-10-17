"""
TTS 专用文本处理模块。

本模块所有函数均为无状态纯函数，不依赖任何全局变量，
可被项目中任意模块安全导入。

包含：
  - EMO_PRESETS                       情绪预设字典（标签名 → VTS 动作）
  - convert_english_abbreviations_to_katakana  英文缩写 → 片假名
  - correct_pronunciation_for_tts     TTS 专用发音修正入口
"""

import re
import unicodedata

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

# 现有缩写 / 品牌名读音；只匹配完整词，大小写变体共用同一条记录。
_SPECIAL_CASES: dict[str, str] = {
    # Consensus / distributed systems terms
    'Paxos': 'パクソス',
    'Raft': 'ラフト',
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
    'Bilibili': 'ビリビリ',
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

# Japanese characters are word characters in Python, so \b cannot delimit an
# initialism next to Japanese. Match complete Latin tokens instead; a known
# spelling such as API must never replace a substring of CAPITAL.
_LATIN_LETTERS = "A-Za-zＡ-Ｚａ-ｚ"
_LATIN_TOKEN_PATTERN = re.compile(rf"[{_LATIN_LETTERS}]+(?:['’＇][{_LATIN_LETTERS}]+)*(?:[0-9０-９]+[{_LATIN_LETTERS}]*)?")
_INITIALISM_PATTERN = re.compile(r"[A-Z]{2,6}(?:[0-9]+[A-Za-z]*)?")
_SPECIAL_CASES_FOLDED = {word.casefold(): reading for word, reading in _SPECIAL_CASES.items()}


def convert_english_abbreviations_to_katakana(text: str) -> str:
    """
    将英文缩写转换为片假名，使 TTS 逐字母正确朗读。

    按完整英文词匹配；普通单词留给日语前端和可选词典处理。
    """
    def _convert(match: re.Match) -> str:
        token = unicodedata.normalize("NFKC", match.group(0))
        known = _SPECIAL_CASES_FOLDED.get(token.casefold())
        if known:
            return known
        if not _INITIALISM_PATTERN.fullmatch(token):
            return token
        return "".join(
            _LETTER_TO_KATAKANA.get(ch.upper(), ch) if ch.isalpha() else ch
            for ch in token
        )

    return _LATIN_TOKEN_PATTERN.sub(_convert, text)


def correct_pronunciation_for_tts(text: str) -> str:
    """
    TTS 发音修正入口。

    当前修正内容：
      - 专有名词读音（如「牧瀬紅莉栖」→「牧瀬クリス」）
      - 英文缩写 → 片假名（调用 convert_english_abbreviations_to_katakana）

    英文单词的读音修复应放在可选词典的数据中；此处保留现有名称兼容。
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

    text = convert_english_abbreviations_to_katakana(text)
    return text
