"""Synthetic public SDK voice used by the optional mixed-theme lifecycle probe."""
import json, math, os, struct
from pathlib import Path
from amadeus_extension import Extension, AudioChunk
VERSION = "VERSION_VALUE"
extension = Extension()
data = None
def event(kind, **details):
    with (data / 'lifecycle.jsonl').open('a', encoding='utf-8') as output:
        output.write(json.dumps(dict(event=kind, version=VERSION, pid=os.getpid(), **details)) + '\n')
@extension.initialize
def initialize(context):
    global data
    data = Path(context.paths['data'])
    event('initialize')
@extension.tts('tone')
def tone(request, context):
    event('synthesize', language=request.language, speed=request.speed, text_length=len(request.text))
    context.check_cancelled()
    pcm = b''.join(struct.pack('<h', int(1000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(4800))
    yield AudioChunk(sample_rate=24000, pcm_s16le=pcm)
    event('completed', samples=4800)
@extension.shutdown
def close():
    if data is not None:
        event('shutdown')
