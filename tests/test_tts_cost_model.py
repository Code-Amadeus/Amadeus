"""Cost observations describe actual profiles, not padding or previous turns."""
from types import SimpleNamespace

import pytest

from tts.deadline import SynthesisCostModel


def test_fixed_overhead_is_not_multiplied_with_text_length():
    model = SynthesisCostModel()
    for chars in (4, 8, 16, 24, 32):
        model.observe('short', chars, 0.3 + 0.02 * chars)
    assert model.predict('short', 24, initial_seconds_per_char=.08) == pytest.approx(.78)
    assert model.predict('short', 40, initial_seconds_per_char=.08) >= 1.1
    assert model.predict('short', 4, initial_seconds_per_char=.08) == pytest.approx(.38)


def test_one_cold_capture_does_not_become_the_rate():
    model = SynthesisCostModel()
    model.observe('short', 4, 10)
    assert model.predict('short', 20, initial_seconds_per_char=.08) == pytest.approx(1.6)
    for chars in (8, 16, 24, 32, 40):
        model.observe('short', chars, .3 + .02 * chars)
    assert model.predict('short', 40, initial_seconds_per_char=.08) == pytest.approx(1.1)
    model.observe('short', 12, 20)
    assert model.predict('short', 40, initial_seconds_per_char=.08) == pytest.approx(1.1)


def test_repeated_short_units_do_not_create_an_overhead_feedback_loop():
    model = SynthesisCostModel()
    for _ in range(20):
        model.observe('short', 4, .4)
    # Equal-length observations cannot distinguish overhead from per-char work.
    # Both .4 seconds fixed and .1 seconds/char explain this same history.
    assert model.predict('short', 40, initial_seconds_per_char=.02) >= 4.0


@pytest.mark.parametrize('lengths', [(20, 20, 20), (20, 21, 20, 21), (20,) * 5])
def test_insufficient_length_evidence_does_not_underestimate_a_slow_engine(lengths):
    model = SynthesisCostModel()
    for chars in lengths:
        model.observe('p', chars, .5 + .2 * chars)
    assert model.predict('p', 40, initial_seconds_per_char=.08) >= 8.5


@pytest.mark.parametrize('times', [(1.7, 1.69, 1.68, 1.67, 1.66), (1.7, 1.701, 1.702, 1.703, 1.704)])
def test_narrow_noisy_samples_cannot_make_long_requests_look_constant_cost(times):
    model = SynthesisCostModel()
    for chars, elapsed in zip(range(20, 25), times):
        model.observe('p', chars, elapsed)
    medium = model.predict('p', 40, initial_seconds_per_char=.08)
    long = model.predict('p', 80, initial_seconds_per_char=.08)
    assert medium >= 2.5
    assert long >= 5.0
    assert long > medium


def test_profile_history_is_independent_and_adapts_to_a_persistent_slowdown():
    model = SynthesisCostModel()
    for chars in (8, 16, 24, 32, 40):
        model.observe('16', chars, .1 + .01 * chars)
    assert model.predict('32', 50, initial_seconds_per_char=.16) == 8.0
    for chars in (8, 16, 24, 32, 40, 8, 16, 24, 32):
        model.observe('16', chars, .4 + .1 * chars)
    assert model.predict('16', 40, initial_seconds_per_char=.08) == pytest.approx(4.4)


@pytest.mark.parametrize('chars,elapsed', [(0, 1), (4, 0), (4, float('nan')), (4, float('inf'))])
def test_invalid_observations_do_not_train(chars, elapsed):
    model = SynthesisCostModel()
    for _ in range(10):
        model.observe('p', chars, elapsed)
    assert model.predict('p', 10, initial_seconds_per_char=.08) == .8


def test_profile_boundary_uses_effective_rocm_parameters(monkeypatch):
    from tts import pipeline
    monkeypatch.setattr(pipeline, '_tts_runtime', SimpleNamespace(is_rocm=False))
    monkeypatch.setattr(pipeline, '_synthesis_cost', SynthesisCostModel())
    monkeypatch.setattr(pipeline, 'correct_pronunciation_for_tts', lambda text, **_kwargs: text)
    monkeypatch.setattr(pipeline, 'TTS_RTF_INITIAL', .6)
    monkeypatch.setattr(pipeline, 'TTS_CHARS_PER_SEC', 7.5)
    assert pipeline.get_sovits_params('あ' * 44)['sample_steps'] == 16
    assert pipeline.get_sovits_params('あ' * 45)['sample_steps'] == 32
    assert pipeline.predict_synthesis_seconds('あ' * 45) == pytest.approx(7.2)
    monkeypatch.setattr(pipeline, '_tts_runtime', SimpleNamespace(is_rocm=True))
    _, params = pipeline._prepare_synthesis_request('あ' * 45)
    assert params['sample_steps'] == 16
    assert params['how_to_cut'] == '凑四句一切'
    assert pipeline.predict_synthesis_seconds('あ' * 45) == pytest.approx(3.6)
    assert pipeline.get_sovits_params('あ' * 45, True)['sample_steps'] == 4


def test_prediction_and_observation_share_prepared_text(monkeypatch):
    from tts import pipeline
    model = SynthesisCostModel()
    monkeypatch.setattr(pipeline, '_synthesis_cost', model)
    monkeypatch.setattr(pipeline, '_tts_runtime', SimpleNamespace(is_rocm=False))
    monkeypatch.setattr(pipeline, 'correct_pronunciation_for_tts', lambda text, **_kwargs: text.replace('ABC', 'あいうえお'))
    for n in (1, 2, 3, 4, 8):
        text, params = pipeline._prepare_synthesis_request('（舞台指示）' + 'ABC' * n)
        pipeline._observe_synthesis(.2 + .01 * len(text), text, params, 'synthetic')
    assert pipeline.predict_synthesis_seconds('（とても長い舞台指示）ABCABC') == pytest.approx(.3)


def test_runtime_replacement_resets_costs(monkeypatch):
    from tts import pipeline
    model = SynthesisCostModel()
    monkeypatch.setattr(pipeline, '_synthesis_cost', model)
    runtime = SimpleNamespace()
    monkeypatch.setattr(pipeline, '_tts_runtime', runtime)
    for _ in range(5):
        model.observe('p', 10, 10)
    pipeline.configure(tts_runtime=runtime)
    assert model.predict('p', 10, initial_seconds_per_char=.08) == 10
    pipeline.configure(tts_runtime=SimpleNamespace())
    assert model.predict('p', 10, initial_seconds_per_char=.08) == .8


@pytest.mark.parametrize('enhanced', [False, True])
@pytest.mark.parametrize('rocm', [False, True])
def test_preparation_preserves_existing_mode_profiles_with_scheduler_disabled(monkeypatch, enhanced, rocm):
    from tts import pipeline
    monkeypatch.setenv('ENABLE_TTS_UTTERANCE_SCHEDULER', '0')
    monkeypatch.setattr(pipeline, '_tts_runtime', SimpleNamespace(is_rocm=rocm))
    raw = 'あ' * 41 + 'AI。'  # 44 -> 46 chars after pronunciation conversion
    prepared, params = pipeline._prepare_synthesis_request(raw, enhanced=enhanced)
    assert len(prepared) == 46
    # Preserve the buffered mode's pre-expansion tier and enhanced mode's
    # post-expansion tier. The ROCm step cap belongs to the buffered mode.
    assert params['sample_steps'] == (32 if enhanced else 16)
    assert params['how_to_cut'] == ('凑四句一切' if enhanced else '不切')
    _, long_params = pipeline._prepare_synthesis_request('あ' * 50, enhanced=enhanced)
    assert long_params['sample_steps'] == (16 if rocm and not enhanced else 32)


def test_legacy_enhanced_terminal_mark_is_preserved():
    from tts import pipeline
    prepared, _ = pipeline._prepare_synthesis_request('確認します', enhanced=True)
    assert prepared == '確認します。'
    prepared, _ = pipeline._prepare_synthesis_request('確認して、', enhanced=True)
    assert prepared == '確認して、'
