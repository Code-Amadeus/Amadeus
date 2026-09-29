import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tts import experimental_emotion as lab
from tts.contract import TTSRequest
from tts.utterance_scheduler import TTSUtteranceScheduler


def test_experiment_off_and_non_windows_are_inert(monkeypatch):
    monkeypatch.delenv(lab.SWITCH, raising=False)
    assert not lab.requested()
    monkeypatch.setenv(lab.SWITCH, "1")
    monkeypatch.setattr(lab.sys, "platform", "linux")
    assert not lab.requested()


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setattr(lab, "requested", lambda: True)
    monkeypatch.setattr(lab, "ROOT", tmp_path)
    root = tmp_path / "assets/audio/reference/emotions"
    root.mkdir(parents=True)
    for name in set(lab.REFERENCE_FILES.values()):
        (root / name).write_bytes(b"reference fixture")
        (root / name).with_suffix(".txt").write_text("参考です。", encoding="utf-8")

    class Base:
        def __init__(self, version="v3", device="cuda:0", rocm=False, weights=True):
            self.model_version, self.device, self.is_rocm = version, device, rocm
            self.gpt_path = "kurisu_v3hc_ja-e13.ckpt"
            self.sovits_path = "kurisu_v3hc_ja_e3_s3861_l32.pth" if weights else "different-v3.pth"
            self.splits = set("。！？")
            self.cache = {}
            self._build_session_cache('default.wav', 'default', 'all_ja')

        def _build_session_cache(self, audio, text, lang):
            if audio not in self.cache:
                self.cache[audio] = {k: (audio, k) for k in (*lab.ACOUSTIC_KEYS, "prompt", "phones1", "bert1")}
            return self.cache[audio]

        def infer_stream(self, **params):
            yield self._build_session_cache(params['ref_audio_path'], params['prompt_text'], 'all_ja')

    return lab.inferencer_type(Base)


@pytest.mark.parametrize("kwargs", [{"version":"v2ProPlus"},{"device":"cpu"},{"rocm":True}])
def test_other_model_device_profiles_do_not_route(engine, kwargs):
    infer = engine(**kwargs)
    assert not infer.experimental_emotion_enabled


def test_v3_routing_does_not_depend_on_checkpoint_filename(engine):
    assert engine(weights=False).experimental_emotion_enabled


def test_disabled_inferencer_does_not_require_emotion_assets(engine, monkeypatch):
    (lab.ROOT / 'assets/audio/reference/emotions/sad.txt').unlink()
    monkeypatch.setattr(lab, 'requested', lambda: False)
    infer = engine()
    assert not infer.experimental_emotion_enabled
    result = list(lab.stream(runtime_for(infer), 'sad', ref_audio_path='default.wav', prompt_text='default'))[0]
    assert result is infer.cache['default.wav']


def runtime_for(infer):
    return SimpleNamespace(backend_id="gpt_sovits",deployment="embedded",
        backend=SimpleNamespace(experimental_emotion_enabled=infer.experimental_emotion_enabled),
        infer_stream=infer.infer_stream)


def test_semantic_reference_changes_without_mutating_acoustic_cache(engine):
    infer = engine();runtime=runtime_for(infer)
    result=list(lab.stream(runtime,'sad',ref_audio_path='default.wav',prompt_text='default'))[0]
    for key in lab.ACOUSTIC_KEYS:assert result[key]==('default.wav',key)
    assert Path(result['prompt'][0]).name=='sad.wav'
    assert infer.cache['default.wav']['prompt']==('default.wav','prompt')
    sad_cache=next(v for k,v in infer.cache.items() if Path(k).name=='sad.wav')
    assert Path(sad_cache['prompt_ge'][0]).name=='sad.wav'
    assert lab._emotion.get()==''
    default=list(lab.stream(runtime,'normal',ref_audio_path='default.wav',prompt_text='default'))[0]
    assert default is infer.cache['default.wav']


def test_stream_close_resets_conditioning_before_next_request(engine):
    infer=engine();runtime=runtime_for(infer)
    stream=lab.stream(runtime,'shy',ref_audio_path='default.wav',prompt_text='default')
    assert Path(next(stream)['prompt'][0]).name=='shy_b.ogg'
    stream.close()
    assert lab._emotion.get()==''


def test_missing_reference_pair_is_observable(engine, monkeypatch):
    (lab.ROOT/'assets/audio/reference/emotions/sad.txt').unlink()
    with pytest.raises(RuntimeError,match='pair missing'):engine()


async def test_scheduler_does_not_merge_across_emotion_boundary(monkeypatch):
    monkeypatch.setenv('ENABLE_TTS_UTTERANCE_SCHEDULER','1')
    monkeypatch.setenv('TTS_UTTERANCE_MIN_START_SEQ','2')
    monkeypatch.setenv('TTS_UTTERANCE_FLUSH_TIMEOUT_MS','10')
    queue=asyncio.Queue()
    for seq,emo,text in [(2,'sad','まず、'),(3,'sad','話すと、'),(4,'normal','結論です。')]:
        queue.put_nowait(TTSRequest(f'sentence_{seq}_test',text,emotion=emo,turn_id='turn'))
    scheduler=TTSUtteranceScheduler()
    a=await scheduler.next_job(queue);b=await scheduler.next_job(queue)
    assert a.emotion=='sad' and a.consumed_count==2
    assert b.emotion=='normal' and b.consumed_count==1


@pytest.mark.parametrize('chunk_size',[1,7,1000])
async def test_main_chat_routes_tags_in_order_and_resets_next_turn(monkeypatch,chunk_size):
    from core.chat_runtime import ChatRuntime
    from tts import pipeline
    monkeypatch.setattr(pipeline,'experimental_emotion_enabled',lambda:True)
    monkeypatch.setattr('core.chat_runtime._pre_translation_enabled',lambda:False)
    expression=SimpleNamespace(register_sentence_actions=Mock())
    monkeypatch.setattr('core.chat_runtime._get_expr_ctrl',lambda:expression)
    runtime=ChatRuntime();queue=asyncio.Queue()
    runtime.configure(pending_sentence_items=queue,playback_manager=SimpleNamespace(mark_turn_last_sentence=Mock()))
    text='最初。[EMO preset=sad dur=4s]悲しい。続きも。[EMO normal]戻る。'
    stream=runtime.begin_role_text_stream(turn_id='one')
    for i in range(0,len(text),chunk_size):await stream.feed(text[i:i+chunk_size])
    await stream.finish()
    items=[]
    while not queue.empty():items.append(queue.get_nowait())
    assert [(r.text,r.emotion) for r in items]==[
        ('最初。','normal'),('悲しい。','sad'),('続きも。','sad'),('戻る。','normal')]
    assert items[0].is_first and not items[1].is_first
    assert all('EMO' not in r.text for r in items)
    next_stream=runtime.begin_role_text_stream(turn_id='two')
    await next_stream.feed('新しい。');await next_stream.finish()
    assert queue.get_nowait().emotion=='normal'


async def test_inline_change_flushes_old_text_not_retroactive(monkeypatch):
    from core.chat_runtime import ChatRuntime
    from tts import pipeline
    monkeypatch.setattr(pipeline,'experimental_emotion_enabled',lambda:True)
    monkeypatch.setattr('core.chat_runtime._pre_translation_enabled',lambda:False)
    monkeypatch.setattr('core.chat_runtime._get_expr_ctrl',lambda:SimpleNamespace(register_sentence_actions=Mock()))
    runtime=ChatRuntime();queue=asyncio.Queue();runtime.configure(pending_sentence_items=queue)
    stream=runtime.begin_role_text_stream(turn_id='mid')
    await stream.feed('前の部分[EMO shy]照れる。');await stream.finish()
    first,second=queue.get_nowait(),queue.get_nowait()
    assert [(first.text,first.emotion),(second.text,second.emotion)]==[
        ('前の部分','normal'),('照れる。','shy')]


async def test_disabled_request_emotion_stays_empty(monkeypatch):
    from core.chat_runtime import ChatRuntime
    from tts import pipeline
    monkeypatch.setattr(pipeline,'experimental_emotion_enabled',lambda:False)
    monkeypatch.setattr('core.chat_runtime._pre_translation_enabled',lambda:False)
    monkeypatch.setattr('core.chat_runtime._get_expr_ctrl',lambda:SimpleNamespace(register_sentence_actions=Mock()))
    runtime=ChatRuntime();queue=asyncio.Queue();runtime.configure(pending_sentence_items=queue)
    await runtime.enqueue_completed_role_text('[EMO sad]悲しい。',turn_id='off')
    assert queue.get_nowait().emotion==''


async def test_routed_opening_bypasses_default_audio_cache(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import AsyncMock
    import numpy as np
    from tts import pipeline
    seen=[]
    def infer_stream(**_kwargs):
        seen.append(lab._emotion.get())
        yield 24000,np.ones(240,dtype=np.float32),'声'
    runtime=SimpleNamespace(backend_id='gpt_sovits',deployment='embedded',is_rocm=False,
        backend=SimpleNamespace(experimental_emotion_enabled=True),infer_stream=infer_stream)
    monkeypatch.setattr(lab,'requested',lambda:True)
    monkeypatch.setattr(pipeline,'current_tts_language_code',lambda:'ja')
    monkeypatch.setattr(pipeline,'_tts_runtime',runtime)
    monkeypatch.setattr(pipeline,'_tts_interrupt_epoch',7)
    monkeypatch.setattr(pipeline,'correct_pronunciation_for_tts',lambda x,**kw:x)
    player=SimpleNamespace(add_to_playlist=AsyncMock())
    monkeypatch.setattr(pipeline,'_playback_manager',player)
    cache=SimpleNamespace(lookup=Mock(return_value=(24000,np.ones(240,dtype=np.float32))),store=Mock())
    monkeypatch.setattr(pipeline,'get_first_sentence_audio_cache',lambda:cache)
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(pipeline,'_tts_executor',executor)
        await pipeline.speak_stream_enhanced_asyncio_queue('声。','sad-id',True,emotion='sad',interrupt_epoch=7)
        cache.lookup.assert_not_called();cache.store.assert_not_called()
        assert seen==['sad']
        await pipeline.speak_stream_enhanced_asyncio_queue('声。','normal-id',True,emotion='normal',interrupt_epoch=7)
        cache.lookup.assert_called_once()
        assert seen==['sad']
