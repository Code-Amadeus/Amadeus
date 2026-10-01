import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import settings
from tts.backend import TTSSynthesisRequest, TTSRuntimeAdapter
from tts.backends import gpt_sovits
from tts.reference_pack import PACK_FORMAT, PACK_ID, PACK_TREE, ReferencePackError, load_reference_pack


ROOT = Path(__file__).resolve().parents[1]


def write_pack(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ('sad', 'shy'):
        (root / (name + '.wav')).write_bytes(b'local fixture')
        (root / (name + '.txt')).write_text('参考です', encoding='utf-8')
    references = {emo: dict(audio=name + '.wav', transcript=name + '.txt')
                  for emo, name in [('sad', 'sad'), ('shy', 'shy'), ('blush', 'shy')]}
    (root / 'references.json').write_text(json.dumps(dict(format=PACK_FORMAT, language='ja', references=references)), encoding='utf-8')


# Exercise the actual cache methods without importing the optional model stack.
source = ast.parse((ROOT / 'local_tts_infer.py').read_text(encoding='utf-8'))
engine_class = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'TTSInferencer')
cache_methods = [node for node in engine_class.body if isinstance(node, ast.FunctionDef)
                 and node.name in {'warm_reference_cache', '_build_synthesis_cache'}]
scope = {}
exec(compile(ast.Module(body=cache_methods, type_ignores=[]), 'local_tts_infer.py', 'exec'), scope)


class CacheEngine:
    model_version = 'v3'
    is_rocm = False
    device = 'cuda:0'
    splits = set('。！？.!?')
    warm_reference_cache = scope['warm_reference_cache']
    _build_synthesis_cache = scope['_build_synthesis_cache']

    def __init__(self):
        self.cache = {}
        self.builds = []
        self._sync_sovits_timing = Mock()

    def _build_session_cache(self, audio, text, language):
        key = (audio, text, language)
        if key not in self.cache:
            self.builds.append(key)
            self.cache[key] = {name: (audio, name) for name in
                ('prompt', 'phones1', 'bert1', 'refer_spec', 'prompt_fea_ref', 'prompt_ge', 'mel2_norm')}
        return self.cache[key]


@pytest.fixture
def backend(monkeypatch, tmp_path):
    monkeypatch.setattr(gpt_sovits, '_PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(gpt_sovits.sys, 'platform', 'win32')
    monkeypatch.setattr(settings, 'ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING', True)
    monkeypatch.setattr(settings, 'TTS_OUTPUT_LANGUAGE', '日文')
    monkeypatch.setattr(settings, 'TTS_REF_AUDIO_JA', 'default.wav')
    monkeypatch.setattr(settings, 'TTS_REF_TEXT_JA', 'Default。')
    write_pack(tmp_path / 'assets' / PACK_TREE)
    result = gpt_sovits.GPTSoVITSBackend()
    result._inferencer = CacheEngine()
    return result


def test_enabled_startup_warms_unique_refs_and_live_cache_key_matches(backend):
    engine = backend._inferencer
    backend._configure_emotion_references()
    assert backend.emotion_reference_status['ready']
    assert backend.emotion_reference_status['warmed_references'] == 2
    assert len(engine.builds) == 3
    assert all(text == '参考です。' and lang == 'all_ja' for _, text, lang in engine.builds[1:])
    assert backend.emotion_reference_key('blush') == backend.emotion_reference_key('shy')
    assert backend.emotion_reference_key('thinking') == ''
    ref = backend._emotion_pack.references['shy']
    engine._build_synthesis_cache('default.wav', 'Default。', 'all_ja', (str(ref.audio), ref.text, 'all_ja'))
    assert len(engine.builds) == 3
    engine._build_synthesis_cache('default.wav', 'Default。', 'all_ja', (str(ref.audio), ref.text, 'all_ja'))
    assert len(engine.builds) == 3


def test_readiness_is_not_published_until_all_warmup_finishes(backend, monkeypatch):
    original = backend._inferencer.warm_reference_cache
    observed = []
    def warm(*args):
        observed.append(backend.emotion_reference_status['ready'])
        return original(*args)
    monkeypatch.setattr(backend._inferencer, 'warm_reference_cache', warm)
    backend._configure_emotion_references()
    assert observed == [False, False, False]
    assert backend.emotion_reference_status['ready']


def test_sidecar_requests_do_not_receive_embedded_emotion_option():
    request = TTSSynthesisRequest('普通。', options={'emotion': 'sad', 'sample_steps': 16})
    serialized = gpt_sovits.GPTSoVITSBackend._serialize_request(request)
    assert serialized['options'] == {'sample_steps': 16}
    assert request.options['emotion'] == 'sad'


def test_request_local_mix_retains_all_default_acoustic_keys(backend):
    backend._configure_emotion_references()
    request = TTSSynthesisRequest('声', language='ja', reference_audio='default.wav', reference_text='Default。', options={'emotion': 'sad'})
    kwargs = backend._request_kwargs(request, streaming=True)
    assert 'emotion' not in kwargs
    engine = backend._inferencer
    mixed = engine._build_synthesis_cache('default.wav', 'Default。', 'all_ja', kwargs['semantic_reference'])
    for key in ('refer_spec', 'prompt_fea_ref', 'prompt_ge', 'mel2_norm'):
        assert mixed[key] == ('default.wav', key)
    assert Path(mixed['prompt'][0]).name == 'sad.wav'
    original = engine.cache[(kwargs['semantic_reference'][0], '参考です。', 'all_ja')]
    assert Path(original['prompt_ge'][0]).name == 'sad.wav'
    normal = backend._request_kwargs(TTSSynthesisRequest('普通', language='ja'), streaming=True)
    assert 'semantic_reference' not in normal
    assert engine._build_synthesis_cache('default.wav', 'Default。', 'all_ja') is engine.cache[('default.wav', 'Default。', 'all_ja')]


def test_semantic_pack_language_is_independent_of_default_acoustic_transcript(backend):
    backend._configure_emotion_references()
    engine = backend._inferencer
    request = TTSSynthesisRequest('日本語。', language='ja', reference_language='en', options={'emotion': 'sad'})
    reference = backend._request_kwargs(request, streaming=True)['semantic_reference']
    assert reference[-1] == 'all_ja'
    count = len(engine.builds)
    engine._build_synthesis_cache('default.wav', 'Default.', 'en', reference)
    assert len(engine.builds) == count + 1  # only the new acoustic-pair key


def test_disabled_needs_no_pack_or_warmup(backend, monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING', False)
    (gpt_sovits._PROJECT_ROOT / 'assets' / PACK_TREE / 'references.json').unlink()
    backend._configure_emotion_references()
    assert backend.emotion_reference_status['state'] == 'disabled'
    assert not backend._inferencer.builds


@pytest.mark.parametrize('profile', ['v2', 'cpu', 'rocm', 'other_os', 'english', 'sidecar'])
def test_unsupported_profiles_do_not_load_or_warm_pack(backend, monkeypatch, profile):
    if profile == 'v2': backend._inferencer.model_version = 'v2'
    if profile == 'cpu': backend._inferencer.device = 'cpu'
    if profile == 'rocm': backend._inferencer.is_rocm = True
    if profile == 'other_os': monkeypatch.setattr(gpt_sovits.sys, 'platform', 'linux')
    if profile == 'english': monkeypatch.setattr(settings, 'TTS_OUTPUT_LANGUAGE', '英文')
    if profile == 'sidecar': backend._inferencer = None
    backend._configure_emotion_references()
    assert backend.emotion_reference_status['state'] == 'unsupported'
    assert backend._emotion_pack is None


def test_missing_or_failed_warmup_keeps_default_backend(backend, monkeypatch):
    engine = backend._inferencer
    monkeypatch.setattr(engine, '_build_session_cache', lambda *args: {})
    backend._configure_emotion_references()
    assert backend.emotion_reference_status['state'] == 'invalid'
    assert backend._inferencer is engine and backend._emotion_pack is None
    assert 'semantic_reference' not in backend._request_kwargs(TTSSynthesisRequest('普通', options={'emotion': 'sad'}), streaming=True)
    (gpt_sovits._PROJECT_ROOT / 'assets' / PACK_TREE / 'references.json').unlink()
    backend._configure_emotion_references()
    assert backend.emotion_reference_status['state'] == 'not_installed'


@pytest.mark.parametrize('bad', ['../outside.wav', '/outside.wav', 'C:/outside.wav', 'sad\\file.wav'])
def test_pack_rejects_escaping_members(backend, bad):
    root = gpt_sovits._PROJECT_ROOT / 'assets' / PACK_TREE
    raw = json.loads((root / 'references.json').read_text())
    raw['references']['sad']['audio'] = bad
    (root / 'references.json').write_text(json.dumps(raw))
    with pytest.raises(ReferencePackError): load_reference_pack(root)


def test_optional_pack_uses_existing_bundle_installer(backend, tmp_path):
    from tools.external_assets import build_bundle, install_bundle
    root = gpt_sovits._PROJECT_ROOT
    archive = tmp_path / 'emotion.zip'
    index = ROOT / 'assets/index.json'
    built = build_bundle(project_root=root, pack_ids=[PACK_ID], output=archive, index_path=index)
    assert built['file_count'] == 5
    target = tmp_path / 'recipient'
    default = target / 'assets/audio/reference/default.wav'
    default.parent.mkdir(parents=True)
    default.write_bytes(b'recipient default')
    installed = install_bundle(archive_path=archive, project_root=target, index_path=index)
    assert installed['installed_files'] == 5
    assert default.read_bytes() == b'recipient default'
    assert len(load_reference_pack(target / 'assets' / PACK_TREE).distinct_references()) == 2


@pytest.mark.parametrize('chunk_size', [1, 7, 1000])
async def test_stream_uses_effective_reference_and_resets_each_turn(backend, monkeypatch, chunk_size):
    from core.chat_runtime import ChatRuntime
    from tts import pipeline
    backend._configure_emotion_references()
    monkeypatch.setattr(pipeline, '_tts_runtime', TTSRuntimeAdapter(backend))
    monkeypatch.setattr(pipeline, 'current_tts_language_code', lambda: 'ja')
    monkeypatch.setattr('core.chat_runtime._pre_translation_enabled', lambda: False)
    expression = SimpleNamespace(register_sentence_actions=Mock())
    monkeypatch.setattr('core.chat_runtime._get_expr_ctrl', lambda: expression)
    queue = asyncio.Queue()
    chat = ChatRuntime()
    chat.configure(pending_sentence_items=queue)
    text = '普通[EMO thinking]に続ける。[EMO shy]照れて[EMO blush]しまう。[EMO normal]戻る。'
    stream = chat.begin_role_text_stream(turn_id='one')
    for i in range(0, len(text), chunk_size): await stream.feed(text[i:i+chunk_size])
    await stream.finish()
    items = []
    while not queue.empty(): items.append(queue.get_nowait())
    assert [(item.text, item.emotion) for item in items] == [('普通に続ける。', ''), ('照れてしまう。', 'shy'), ('戻る。', '')]
    assert expression.register_sentence_actions.called
    stream = chat.begin_role_text_stream(turn_id='two')
    await stream.feed('次の輪。'); await stream.finish()
    assert queue.get_nowait().emotion == ''


async def test_disabled_stream_retains_default_request(backend, monkeypatch):
    from core.chat_runtime import ChatRuntime
    from tts import pipeline
    monkeypatch.setattr(pipeline, '_tts_runtime', TTSRuntimeAdapter(backend))
    monkeypatch.setattr('core.chat_runtime._pre_translation_enabled', lambda: False)
    monkeypatch.setattr('core.chat_runtime._get_expr_ctrl', lambda: SimpleNamespace(register_sentence_actions=Mock()))
    queue = asyncio.Queue(); chat = ChatRuntime(); chat.configure(pending_sentence_items=queue)
    await chat.enqueue_completed_role_text('[EMO sad]悲しい。', turn_id='off')
    assert queue.get_nowait().emotion == ''


async def test_scheduler_merges_aliases_but_stops_at_changed_reference(backend, monkeypatch):
    from tts.contract import TTSRequest
    from tts.utterance_scheduler import TTSUtteranceScheduler
    backend._configure_emotion_references()
    monkeypatch.setenv('ENABLE_TTS_UTTERANCE_SCHEDULER', '1')
    monkeypatch.setenv('TTS_UTTERANCE_MIN_START_SEQ', '2')
    monkeypatch.setenv('TTS_UTTERANCE_FLUSH_TIMEOUT_MS', '10')
    queue = asyncio.Queue()
    for seq, emo, text in [(2, 'shy', '照れて、'), (3, 'blush', '続けて、'), (4, 'sad', '終わる。')]:
        queue.put_nowait(TTSRequest(f'sentence_{seq}_test', text, turn_id='one', emotion=backend.emotion_reference_key(emo)))
    scheduler = TTSUtteranceScheduler()
    first = await scheduler.next_job(queue)
    second = await scheduler.next_job(queue)
    assert first.consumed_count == 2 and first.emotion == 'shy'
    assert second.consumed_count == 1 and second.emotion == 'sad'


async def test_mixed_opening_bypasses_default_audio_cache_and_normal_reuses_it(backend, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import AsyncMock
    import numpy as np
    from tts import pipeline
    backend._configure_emotion_references()
    seen = []
    def infer_stream(**kwargs):
        seen.append(kwargs.get('emotion', ''))
        yield 24000, np.ones(240, dtype=np.float32), '声'
    runtime = TTSRuntimeAdapter(backend)
    monkeypatch.setattr(runtime, 'infer_stream', infer_stream)
    monkeypatch.setattr(pipeline, '_tts_runtime', runtime)
    monkeypatch.setattr(pipeline, 'current_tts_language_code', lambda: 'ja')
    monkeypatch.setattr(pipeline, '_tts_interrupt_epoch', 7)
    monkeypatch.setattr(pipeline, 'correct_pronunciation_for_tts', lambda x: x)
    monkeypatch.setattr(pipeline, '_playback_manager', SimpleNamespace(add_to_playlist=AsyncMock()))
    cache = SimpleNamespace(lookup=Mock(return_value=(24000, np.ones(240, dtype=np.float32))), store=Mock())
    monkeypatch.setattr(pipeline, 'get_first_sentence_audio_cache', lambda: cache)
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(pipeline, '_tts_executor', executor)
        await pipeline.speak_stream_enhanced_asyncio_queue('声。', 'sad-id', True, emotion='sad', interrupt_epoch=7)
        cache.lookup.assert_not_called(); cache.store.assert_not_called()
        assert seen == ['sad']
        await pipeline.speak_stream_enhanced_asyncio_queue('声。', 'normal-id', True, interrupt_epoch=7)
        cache.lookup.assert_called_once()
        assert seen == ['sad']


def test_startup_gui_exposes_boolean_and_disabled_is_healthy(monkeypatch):
    from server.handlers.system_handler import _voice_configuration
    monkeypatch.setattr(settings, 'ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING', False)
    group = next(group for group in _voice_configuration(settings) if group['id'] == 'tts_emotion_references')
    assert group['status_ok'] and group['status'] == 'disabled'
    field = group['fields'][0]
    assert field['type'] == 'boolean' and field['value'] is False and field['restart_required']
