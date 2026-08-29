import os, sys
import threading

from tqdm import tqdm
import re
import torch
from text.LangSegmenter import LangSegmenter
from text import chinese
from typing import Dict, List, Tuple
from text.cleaner import clean_text
from text import cleaned_text_to_sequence
from transformers import AutoModelForMaskedLM, AutoTokenizer
from TTS_infer_pack.text_segmentation_method import split_big_text, splits, get_method as get_seg_method
from tools.i18n.i18n import I18nAuto, scan_language_list

# ===== 全局设置 =====
language = os.environ.get("language", "Auto")
language = sys.argv[-1] if sys.argv[-1] in scan_language_list() else language
i18n = I18nAuto(language=language)
punctuation = set(['!', '?', '…', ',', '.', '-'])

# ===== 辅助函数 =====
def get_first(text: str) -> str:
    pattern = "[" + "".join(re.escape(sep) for sep in splits) + "]"
    text = re.split(pattern, text)[0].strip()
    return text

def merge_short_text_in_array(texts: List[str], threshold: int) -> List[str]:
    if len(texts) < 2:
        return texts
    result = []
    text = ""
    for ele in texts:
        text += ele
        if len(text) >= threshold:
            result.append(text)
            text = ""
    if len(text) > 0:
        if len(result) == 0:
            result.append(text)
        else:
            result[-1] += text
    return result

# ===== 核心处理类 =====
class TextPreprocessor:
    def __init__(self, bert_model: AutoModelForMaskedLM,
                 tokenizer: AutoTokenizer, device: torch.device):
        self.bert_model = bert_model
        self.tokenizer = tokenizer
        self.device = device
        self.bert_lock = threading.RLock()

    def preprocess(self, text: str, lang: str, text_split_method: str, version: str = "v2") -> List[Dict]:
        print(f'############ {i18n("切分文本")} ############')
        text = self.replace_consecutive_punctuation(text)
        texts = self.pre_seg_text(text, lang, text_split_method)
        result = []
        print(f'############ {i18n("提取文本Bert特征")} ############')
        for text in tqdm(texts):
            phones, bert_features, norm_text = self.segment_and_extract_feature_for_text(text, lang, version)
            if phones is None or norm_text == "":
                continue
            res = {
                "phones": phones,
                "bert_features": bert_features,
                "norm_text": norm_text,
            }
            result.append(res)
        return result

    def pre_seg_text(self, text: str, lang: str, text_split_method: str) -> List[str]:
        text = text.strip("\n")
        if len(text) == 0:
            return []
        if text[0] not in splits and len(get_first(text)) < 4:
            text = "。" + text if lang != "en" else "." + text
        print(i18n("实际输入的目标文本:"))
        print(text)

        seg_method = get_seg_method(text_split_method)
        text = seg_method(text)

        while "\n\n" in text:
            text = text.replace("\n\n", "\n")

        _texts = text.split("\n")
        _texts = self.filter_text(_texts)
        _texts = merge_short_text_in_array(_texts, 5)
        texts = []

        for text in _texts:
            if len(text.strip()) == 0:
                continue
            if not re.sub("\W+", "", text):
                continue
            if text[-1] not in splits:
                text += "。" if lang != "en" else "."

            if len(text) > 510:
                texts.extend(split_big_text(text))
            else:
                texts.append(text)

        print(i18n("实际输入的目标文本(切句后):"))
        print(texts)
        return texts

    def segment_and_extract_feature_for_text(self, text: str, language: str, version: str = "v1") -> Tuple[List, torch.Tensor, str]:
        return self.get_phones_and_bert(text, language, version)

    def get_phones_and_bert(self, text: str, language: str, version: str, final: bool = False):
        with self.bert_lock:
            if language in {"en", "all_zh", "all_ja", "all_ko", "all_yue"}:
                formattext = text.replace("  ", " ")
                if language == "all_zh":
                    if re.search(r'[A-Za-z]', formattext):
                        formattext = re.sub(r'[a-z]', lambda x: x.group(0).upper(), formattext)
                        formattext = chinese.mix_text_normalize(formattext)
                        return self.get_phones_and_bert(formattext, "zh", version)
                    else:
                        phones, word2ph, norm_text = self.clean_text_inf(formattext, language, version)
                        bert = self.get_bert_feature(norm_text, word2ph).to(self.device)
                elif language == "all_yue" and re.search(r'[A-Za-z]', formattext):
                    formattext = re.sub(r'[a-z]', lambda x: x.group(0).upper(), formattext)
                    formattext = chinese.mix_text_normalize(formattext)
                    return self.get_phones_and_bert(formattext, "yue", version)
                else:
                    phones, word2ph, norm_text = self.clean_text_inf(formattext, language, version)
                    bert = torch.zeros((1024, len(phones)), dtype=torch.float32).to(self.device)

            elif language in {"zh", "ja", "ko", "yue", "auto", "auto_yue"}:
                textlist = []
                langlist = []
                if language == "auto" or language == "auto_yue":
                    for tmp in LangSegmenter.getTexts(text):
                        if isinstance(tmp, tuple):
                            tmp = {"lang": tmp[0], "text": tmp[1]}
                        langlist.append("yue" if language == "auto_yue" and tmp["lang"] == "zh" else tmp["lang"])
                        textlist.append(tmp["text"])
                else:
                    for tmp in LangSegmenter.getTexts(text):
                        if isinstance(tmp, tuple):
                            tmp = {"lang": tmp[0], "text": tmp[1]}
                        langlist.append(tmp["lang"] if tmp["lang"] == "en" else language)
                        textlist.append(tmp["text"])

                phones_list = []
                bert_list = []
                norm_text_list = []
                for i in range(len(textlist)):
                    lang = langlist[i]
                    phones, word2ph, norm_text = self.clean_text_inf(textlist[i], lang, version)
                    bert = self.get_bert_inf(phones, word2ph, norm_text, lang)
                    phones_list.append(phones)
                    norm_text_list.append(norm_text)
                    bert_list.append(bert)
                bert = torch.cat(bert_list, dim=1)
                phones = sum(phones_list, [])
                norm_text = ''.join(norm_text_list)

            if not final and len(phones) < 6:
                return self.get_phones_and_bert("." + text, language, version, final=True)

            return phones, bert, norm_text

    def get_bert_feature(self, text: str, word2ph: List) -> torch.Tensor:
        with torch.no_grad():
            inputs = self.tokenizer(text, return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            res = self.bert_model(**inputs, output_hidden_states=True)
            res = torch.cat(res.hidden_states[-3:-2], -1)[0].cpu()[1:-1]
        assert len(word2ph) == len(text)
        phone_level_feature = []
        for i in range(len(word2ph)):
            repeat_feature = res[i].repeat(word2ph[i], 1)
            phone_level_feature.append(repeat_feature)
        return torch.cat(phone_level_feature, dim=0).T

    def clean_text_inf(self, text: str, language: str, version: str = "v2") -> Tuple[List, List, str]:
        language = language.replace("all_", "")
        phones, word2ph, norm_text = clean_text(text, language, version)
        phones = cleaned_text_to_sequence(phones, version)
        return phones, word2ph, norm_text

    def get_bert_inf(self, phones: List, word2ph: List, norm_text: str, language: str) -> torch.Tensor:
        language = language.replace("all_", "")
        if language == "zh":
            feature = self.get_bert_feature(norm_text, word2ph).to(self.device)
        else:
            feature = torch.zeros((1024, len(phones)), dtype=torch.float32).to(self.device)
        return feature

    def filter_text(self, texts: List[str]) -> List[str]:
        return [text for text in texts if text not in [None, "", " ", "\n"]]

    def replace_consecutive_punctuation(self, text: str) -> str:
        punctuations = ''.join(re.escape(p) for p in punctuation)
        pattern = f'([{punctuations}])([{punctuations}])+'
        return re.sub(pattern, r'\1', text)
