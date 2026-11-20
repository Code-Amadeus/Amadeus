import re



def split_lang(text: str) -> list:
    """
    简单分割：根据语言类型（中文/英文/日文）将文本分片。
    返回：['片段1', '片段2', ...]
    """
    if not text:
        return []

    pattern = re.compile(r'[\u4e00-\u9fff]+|[\u3040-\u30ff\u31f0-\u31ff]+|[A-Za-z]+|\d+|[^\w\s]')
    matches = pattern.findall(text)

    result = []
    buffer = ""
    last_lang = None

    for chunk in matches:
        lang = detect_language(chunk)
        if lang == last_lang or last_lang is None:
            buffer += chunk
        else:
            if buffer:
                result.append(buffer)
            buffer = chunk
        last_lang = lang

    if buffer:
        result.append(buffer)

    return result

def detect_language(text: str) -> str:
    """
    检测单个片段属于中文/日文/英文/其他
    """
    if re.search(r'[\u4e00-\u9fff]', text):
        return 'zh'
    elif re.search(r'[\u3040-\u30ff\u31f0-\u31ff]', text):
        return 'ja'
    elif re.search(r'[A-Za-z]', text):
        return 'en'
    else:
        return 'other'

def guess_language(text: str) -> str:
    """
    根据字符数量猜测文本主语言
    """
    if not text:
        return "unknown"

    zh = len(re.findall(r'[\u4e00-\u9fff]', text))
    ja = len(re.findall(r'[\u3040-\u30ff\u31f0-\u31ff]', text))
    en = len(re.findall(r'[A-Za-z]', text))

    counts = {"zh": zh, "ja": ja, "en": en}
    main_lang = max(counts, key=counts.get)
    return main_lang

class LangSplitter:
    """
    高级分割器：返回(语言类型, 片段)对
    """
    def __init__(self):
        self.pattern = re.compile(
            "|".join(
                f"(?P<{k}>{v})" for k, v in {
                    "zh": r'[\u4e00-\u9fff]',
                    "ja": r'[\u3040-\u30ff\u31f0-\u31ff\uFF66-\uFF9D]',
                    "en": r'[A-Za-z]',
                    "num": r'[0-9]',
                    "symbol": r'[^\w\s]'
                }.items()
            )
        )

    def split(self, text: str):
        """
        返回 [(语言, 片段), ...]
        """
        results = []
        current_lang = None
        current_text = ""

        for match in self.pattern.finditer(text):
            groupdict = match.groupdict()
            for lang, value in groupdict.items():
                if value:
                    if current_lang == lang:
                        current_text += value
                    else:
                        if current_text:
                            results.append((current_lang, current_text))
                        current_lang = lang
                        current_text = value
                    break

        if current_text:
            results.append((current_lang, current_text))
        return results

# =================== 测试区块 ====================
if __name__ == "__main__":
    sample_text = "你好，world！これはテスト123です。"

    print("\n--- split_lang ---")
    print(split_lang(sample_text))

    print("\n--- guess_language ---")
    print(guess_language(sample_text))

    print("\n--- LangSplitter ---")
    splitter = LangSplitter()
    for lang, frag in splitter.split(sample_text):
        print(f"[{lang}] {frag}")
