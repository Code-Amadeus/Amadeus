"""MIT-0 offline author example, using only the delivered SDK and standard library."""

import math
import struct
import time
from amadeus_extension import AudioChunk, Extension

extension = Extension()


@extension.tts("tone")
def synthesize(request, context):
    pcm = b"".join(
        struct.pack("<h", int(1000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(2400)
    )
    for _ in range(30):
        context.check_cancelled()
        yield AudioChunk(sample_rate=24000, pcm_s16le=pcm)
        time.sleep(0.02)
