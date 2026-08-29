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

# 常见缩写 / 品牌名特殊映射（按长度降序排列，确保长的先匹配）
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

# 按长度降序排列，确保长缩写先匹配
_SPECIAL_CASES_SORTED = sorted(_SPECIAL_CASES.items(), key=lambda x: len(x[0]), reverse=True)

# 通用大写缩写正则（2-6 个大写字母，可带数字后缀）
_ABBR_PATTERN = re.compile(r'\b[A-Z]{2,6}(?:[0-9]+[A-Z]*)?\b')


def convert_english_abbreviations_to_katakana(text: str) -> str:
    """
    将英文缩写转换为片假名，使 TTS 逐字母正确朗读。

    处理顺序：
      1. 特殊映射（优先级最高，按长度降序）
      2. 通用大写缩写（2-6 字母）逐字母转换
    """
    # 1. 特殊映射
    for abbr, katakana in _SPECIAL_CASES_SORTED:
        text = text.replace(abbr, katakana)

    # 2. 通用大写缩写
    def _convert(match: re.Match) -> str:
        return "".join(
            _LETTER_TO_KATAKANA.get(ch.upper(), ch) if ch.isalpha() else ch
            for ch in match.group(0)
        )

    return _ABBR_PATTERN.sub(_convert, text)


def correct_pronunciation_for_tts(text: str) -> str:
    """
    TTS 发音修正入口。

    当前修正内容：
      - 专有名词读音（如「牧瀬紅莉栖」→「牧瀬クリス」）
      - 英文缩写 → 片假名（调用 convert_english_abbreviations_to_katakana）

    如需添加新的修正，在 replacements 字典中追加即可。
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
