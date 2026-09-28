"""Check the text that public inference passes to the phoneme frontend."""
import os
from types import SimpleNamespace

import pytest

if os.environ.get("AMADEUS_E2E_NO_TTS", "").lower() in {"1", "true", "yes", "on"}:
    pytest.skip("local TTS dependencies are disabled", allow_module_level=True)
torch = pytest.importorskip("torch")
pytest.importorskip("librosa")
from local_tts_infer import TTSInferencer  # noqa: E402


class FrontendReached(BaseException):
    """Stop at the text boundary, before any model or weight is needed."""


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("cut", ["不切", "按标点符号切"])
@pytest.mark.parametrize("ending", ["、", ",", "，", "。", "？", "！", ""])
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
