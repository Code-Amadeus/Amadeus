# ========== langsegmenter.py for v3 ===========

import re

class LangSegmenter:
    DEFAULT_LANG_MAP = {
        'zh': r'[\u4e00-\u9fff]',
        'ja': r'[\u3040-\u30ff\u31f0-\u31ff]',
        'en': r'[A-Za-z]',
        'num': r'[0-9]',
        'other': r'.'
    }

    @staticmethod
    def getTexts(text: str):
        lang_splitter = LangSplitter()
        substrings = lang_splitter.split_by_lang(text=text)
        return substrings

class LangSplitter:
    def __init__(self, lang_map=None):
        if lang_map is None:
            lang_map = LangSegmenter.DEFAULT_LANG_MAP
        self.lang_map = lang_map
        self.compiled_patterns = {lang: re.compile(pattern) for lang, pattern in self.lang_map.items()}

    def split_by_lang(self, text):
        substrings = []
        current_lang = None
        current_text = ''

        for char in text:
            lang = self._get_char_lang(char)
            if lang != current_lang:
                if current_text:
                    substrings.append((current_text, current_lang))
                current_text = char
                current_lang = lang
            else:
                current_text += char

        if current_text:
            substrings.append((current_text, current_lang))

        return substrings

    def _get_char_lang(self, char):
        for lang, pattern in self.compiled_patterns.items():
            if pattern.match(char):
                return lang
        return 'other'
