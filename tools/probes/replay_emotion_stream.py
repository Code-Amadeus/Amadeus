"""Replay captured role output through production ChatRuntime/TTS, with a WAV sink."""
from pathlib import Path
import asyncio, json, logging, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
OUT = None
SOURCE_TURN = 'portable-emotion-replay'

class CaptureQueue(asyncio.Queue):
    def __init__(self):super().__init__();self.requests=[]
    def put_nowait(self,item):
        self.requests.append(dict(sentence_id=item.sentence_id,text=item.text,emotion=item.emotion,is_first=item.is_first))
        super().put_nowait(item)

class WavPlayer:
    """Only output-device substitution; retain wall-clock playback cover."""
    def __init__(self):
        self._hooks=SimpleNamespace(subtitle_available=False,update_subtitle_display=None,check_and_display_pre_translation=None)
        self.jobs=[];self.audio=[];self.sr=None;self.origin=None;self.end=0.0
        self.mouth_sink=SimpleNamespace(publish_mouth_value=lambda value:None)
        self.is_playing=True;self.sample_rate=24000
        self.manager=None
    def initialize(self,sr):
        self.sample_rate=sr;self.is_playing=True
    async def write_audio_async(self,audio,**kwargs):
        # Production first-sentence streaming uses this lower-level output port.
        if not kwargs.get('is_current',lambda:True)():return
        for name in ('before_write','after_first_write'):
            callback=kwargs.get(name)
            if callback:callback()
        callback=kwargs.get('before_window')
        if callback:callback(audio)
        ready=asyncio.Event()
        await self.play_full_audio_and_signal_completion(audio,kwargs.get('sample_rate',self.sample_rate),self.manager.current_playing_id,'[first-sentence streaming chunk]',ready,is_current=kwargs.get('is_current',lambda:True))
    async def play_full_audio_and_signal_completion(self,audio,sr,sentence_id,text,ready,**kwargs):
        import numpy as np
        import soundfile as sf
        try:
            assert kwargs.get('is_current',lambda:True)()
            assert np.isfinite(audio).all() and len(audio)>0
            if self.sr is None:self.sr=sr
            assert self.sr==sr
            now=time.monotonic()
            if self.origin is None:self.origin=now
            start=now-self.origin
            gap=max(0.0,start-self.end)
            if gap:self.audio.append(np.zeros(round(gap*sr),dtype=np.float32))
            samples=np.asarray(audio,dtype=np.float32).copy()
            name=f'{len(self.jobs)+1:03d}.wav'
            sf.write(OUT/name,samples,sr,subtype='FLOAT')
            self.audio.append(samples)
            self.jobs.append(dict(file=name,sentence_id=sentence_id,text=text,sample_rate=sr,samples=len(samples),start_seconds=start,gap_seconds=gap,segments=kwargs.get('subtitle_segments')))
            self.end=start+len(samples)/sr
            print(json.dumps({'stage':'saved_job','job':len(self.jobs),'seconds':round(len(samples)/sr,2)}),flush=True)
            await asyncio.sleep(len(samples)/sr)
        finally:ready.set()

async def main(raw, args):
    import numpy as np
    import soundfile as sf
    from tts.registry import create_tts_runtime
    from tts import pipeline
    from tts.playback import PlaybackManager
    from tts.pre_translation_runtime import runtime as translations
    from core.chat_runtime import ChatRuntime
    translations.configure(False) # no subtitle translation network calls for an audio export
    engine=create_tts_runtime('gpt_sovits')
    player=WavPlayer();playback=PlaybackManager(player);queue=CaptureQueue();done=asyncio.Event()
    player.manager=playback
    started=[];ended=[]
    playback.on_sentence_start=started.append
    playback.on_sentence_complete=lambda sid,text:ended.append(sid)
    playback.on_turn_playback_complete=done.set
    with ThreadPoolExecutor(max_workers=2) as executor:
        pipeline.configure(tts_runtime=engine,tts_executor=executor,playback_manager=playback,player=player,pending_sentence_items=queue,exp_tts_semaphore=asyncio.Semaphore(1))
        assert pipeline.experimental_emotion_enabled() == (args.emotion_routing == "on")
        await pipeline.warmup_graph_pipeline()
        chat=ChatRuntime();chat.configure(pending_sentence_items=queue,playback_manager=playback)
        tasks=[asyncio.create_task(pipeline.play_sentence_worker()),asyncio.create_task(playback.run())]
        try:
            stream=chat.begin_role_text_stream(turn_id='export-'+SOURCE_TURN)
            # Original LLM token-arrival events were not persisted. This is a new
            # streaming replay, not a claim to reproduce the original timing.
            for i in range(0,len(raw),args.chunk_chars):
                await stream.feed(raw[i:i+args.chunk_chars]);await asyncio.sleep(args.interval_ms / 1000)
            await stream.finish()
            (OUT/'requests.json').write_text(json.dumps(queue.requests,ensure_ascii=False,indent=2),encoding='utf-8')
            await asyncio.wait_for(done.wait(),600)
            expected={r['sentence_id'] for r in queue.requests}
            assert expected==set(started)==set(ended), (len(expected),len(started),len(ended))
            all_audio=np.concatenate(player.audio)
            sf.write(OUT/'full-turn.wav',all_audio,player.sr,subtype='PCM_24')
            result=dict(source_turn=SOURCE_TURN,recreated=True,original_audio=False,
                pipeline='ChatRuntime.begin_role_text_stream -> play_sentence_worker -> TTSUtteranceScheduler -> GPTSoVITSBackend -> PlaybackManager -> WAV output sink',
                input_timing=dict(chunk_chars=args.chunk_chars,interval_ms=args.interval_ms,original_timing=False),
                voice_profile=args.voice_profile,
                subtitles_translation=False,duration_seconds=len(all_audio)/player.sr,sample_rate=player.sr,
                sentences=len(expected),started=len(started),ended=len(ended),jobs=player.jobs,
                routing_switch=os.environ.get('ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING'),
                cuda_graph=os.environ.get('ENABLE_CUDA_GRAPH'))
            (OUT/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'stage':'complete','duration':result['duration_seconds'],'sentences':len(expected),'jobs':len(player.jobs),'file':str(OUT/'full-turn.wav')}),flush=True)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

def cli():
    import argparse
    global OUT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text-file',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--emotion-routing',choices=('on','off'),default='off')
    parser.add_argument('--voice-profile',choices=('configured','kurisu_v3','hc3'),default='configured')
    parser.add_argument('--chunk-chars',type=int,default=8)
    parser.add_argument('--interval-ms',type=float,default=100)
    args=parser.parse_args()
    if sys.platform!='win32':parser.error('This experiment is scoped to Windows CUDA V3.')
    if args.chunk_chars<1 or args.interval_ms<0:parser.error('Invalid stream pacing.')
    text_file=args.text_file.resolve()
    OUT=args.output_dir.resolve()
    if OUT.exists() and any(OUT.iterdir()):parser.error('Output directory must be empty to keep run evidence separate.')
    OUT.mkdir(parents=True,exist_ok=True)
    raw=text_file.read_text(encoding='utf-8-sig')
    if not raw.strip():parser.error('Empty text.')
    # Explicit per-process settings; never modifies the desktop .env or session.
    os.environ['ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING']='1' if args.emotion_routing=='on' else '0'
    os.environ.update(TTS_BACKEND='gpt_sovits',TTS_DEVICE='cuda:0',TTS_MODE='embedded',TTS_PYTHON='',
        TTS_OUTPUT_LANGUAGE='日文',ENABLE_CUDA_GRAPH='1',EXP_TTS_MAX_CONCURRENCY='1',
        ENABLE_TTS_UTTERANCE_SCHEDULER='1',AEC_DEBUG_CAPTURE='0')
    if args.voice_profile=='kurisu_v3':os.environ['TTS_VOICE_PROFILE']='kurisu_v3'
    elif args.voice_profile=='hc3':
        os.environ.update(TTS_VOICE_PROFILE='custom',
            TTS_GPT_MODEL_PATH=str(ROOT/'assets/models/gpt-sovits/weights/gpt/v3/kurisu_v3hc_ja-e13.ckpt'),
            TTS_SOVITS_MODEL_PATH=str(ROOT/'assets/models/gpt-sovits/weights/sovits/v3/kurisu_v3hc_ja_e3_s3861_l32.pth'))
    (OUT/'source-with-emotions.txt').write_text(raw,encoding='utf-8')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
    asyncio.run(main(raw,args))

if __name__=='__main__':cli()
