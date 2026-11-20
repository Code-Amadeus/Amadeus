"""Check the text that public inference passes to the phoneme frontend."""
import os
from types import SimpleNamespace

import pytest

if os.environ.get("AMADEUS_E2E_NO_TTS", "").lower() in {"1", "true", "yes", "on"}:
    pytest.skip("local TTS dependencies are disabled", allow_module_level=True)
torch = pytest.importorskip("torch")
pytest.importorskip("librosa")
from local_tts_infer import TTSInferencer, cut1, cut2, cut4, cut5, split  # noqa: E402


class FrontendReached(BaseException):
    """Stop at the text boundary, before any model or weight is needed."""


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("cut", ["不切", "按标点符号切"])
@pytest.mark.parametrize("ending", ["、", ",", "，", "。", "?", "？", "！", ""])
def test_existing_punctuation_is_preserved_before_phoneme_conversion(stream, cut, ending):
    engine = TTSInferencer.__new__(TTSInferencer)
    engine.i18n = lambda text: text
    engine._detect_model_version = lambda: "v2"
    engine._init_language_dict()
    engine.device = "cpu"
    engine.is_half = False
    engine.max_sec = 10
    engine.hps = SimpleNamespace(data=SimpleNamespace(sampling_rate=24000))
    references = []

    def session(path, text, language):
        references.append(text)
        return {"prompt": torch.zeros(1, 1), "phones1": [1], "bert1": torch.zeros(1024, 1)}

    engine._build_session_cache = session
    observed = []

    def frontend(text, language):
        observed.append(text)
        raise FrontendReached

    engine.get_phones_and_bert = frontend
    text = "説明を始めます" + ending
    prompt = "参照音声" + ending
    target = text + "次の項目を確認します。" if cut == "按标点符号切" and ending else text
    with pytest.raises(FrontendReached):
        kwargs = dict(text=target, prompt_text=prompt, ref_audio_path="reference.wav", how_to_cut=cut,
                      text_language="日文", prompt_language="日文")
        if stream:
            list(engine.infer_stream(**kwargs))
        else:
            engine.infer(**kwargs)
    assert observed == [text if ending else text + "。"]
    assert references == [prompt if ending else prompt + "。"]


@pytest.mark.parametrize("splitter,text,expected", [
    (split, "This is a test.", ["This is a test."]),
    (split, "ssss", ["ssss。"]),
    (split, "Is this safe? Yes.", ["Is this safe?", " Yes."]),
    (split, "First?! Next.", ["First?", "!", " Next."]),
    (split, "最初です？次です！", ["最初です？", "次です！"]),
    (cut5, "This is a test.", "This is a test."),
    (cut5, "ssss", "ssss"),
    (cut5, "Sss sss.", "Sss sss."),
    (cut5, "Is this safe? Yes.", "Is this safe?\n Yes."),
    (cut5, "This is 1.25? Yes, it is.", "This is 1.25?\n Yes,\n it is."),
    (cut5, "First?! Next...", "First?\n Next."),
    (cut5, "最初です？次です！", "最初です？\n次です！"),
    (cut4, "First. Second.", "First\n Second"),
    (cut4, "Value 1.25 is fine. Next.", "Value 1.25 is fine\n Next"),
    (cut4, "3.1415", "3.1415"),
    (cut4, "First... Second.", "First\n Second"),
    (cut1, "This is a simple sentence, so this is a second sentence.",
           "This is a simple sentence, so this is a second sentence."),
])
def test_splitters_recognize_punctuation_instead_of_letters(splitter, text, expected):
    assert splitter(text) == expected


def test_four_sentence_grouping_keeps_words_with_s_intact():
    sentences = [f"Sentence {index} stays." for index in range(9)]
    assert cut1(" ".join(sentences)) == " ".join(sentences[:4]) + "\n " + " ".join(sentences[4:])


def test_fifty_character_grouping_cuts_at_punctuation_not_inside_words():
    first = "This sentence stays intact while its words contain several lowercase esses."
    second = "Another sentence also stays intact because letters are not punctuation."
    assert cut2(first + " " + second) == first + "\n " + second
