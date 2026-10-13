"""Contracts for the isolated video experiment, without invoking an encoder."""
import importlib.util
from pathlib import Path
import struct

import numpy as np
import pytest

pytest.importorskip('cv2', reason='Optional offline video probe requires OpenCV')

spec = importlib.util.spec_from_file_location('frame_video_encode', Path(__file__).parents[1]/'tools/probes/frame-video/encode.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_odd_canvas_padding_preserves_authored_pixels_and_alpha_offset():
    source = np.zeros((5, 7, 4), dtype=np.uint8)
    source[1:4, 1:6] = [140, 80, 30, 128]
    packed, layout = module.pack_frame(source)
    assert packed.shape == (6, 24, 3)
    assert layout['alphaX'] == 16
    np.testing.assert_array_equal(packed[1:4,1:6], source[1:4,1:6,:3])
    np.testing.assert_array_equal(packed[:5,16:23,0], source[...,3])
    assert np.all(packed[:5,:7][source[...,3]==0] == [140,80,30])


def test_ivf_index_requires_packet_sizes_counts_and_complete_payloads():
    header = bytearray(32)
    header[:4] = b'DKIF'; header[8:12] = b'VP90'
    struct.pack_into('<HH',header,4,0,32); struct.pack_into('<I',header,24,1)
    data = bytes(header)+struct.pack('<IQ',3,0)+b'abc'
    assert module.index_ivf(data,[{'size':'3','flags':'K_'}],1) == [dict(sourceIndex=0,offset=44,size=3,key=True)]
    with pytest.raises(ValueError, match='Truncated'):
        module.index_ivf(data[:-1],[{'size':'3','flags':'K_'}],1)
    with pytest.raises(ValueError, match='boundary'):
        module.index_ivf(data,[{'size':'2','flags':'K_'}],1)


def test_capture_metrics_expose_background_leaks_without_modifying_alpha():
    target = Path(__file__).parents[1]/'tools/probes/frame-video/compare_captures.py'
    metrics_spec = importlib.util.spec_from_file_location('capture_metrics',target)
    metrics = importlib.util.module_from_spec(metrics_spec)
    metrics_spec.loader.exec_module(metrics)
    source = np.zeros((32,32,4),dtype=np.uint8)
    source[8:24,8:24] = [120,50,200,255]
    exact = metrics.compare(source,source)
    assert exact['visibleCompositeLumaSSIM'] == pytest.approx(1)
    assert exact['alphaMaxAbsError'] == 0
    actual = source.copy(); actual[0,0,3] = 3
    measured = metrics.compare(source,actual)
    assert measured['zeroMax'] == 3
    assert measured['zeroLeakFraction'] > 0


def test_trace_summary_separates_diagnostic_readbacks_from_playback():
    target = Path(__file__).parents[1]/'tools/probes/frame-video/summarize_trace.py'
    trace_spec = importlib.util.spec_from_file_location('trace_metrics',target)
    trace = importlib.util.module_from_spec(trace_spec)
    trace_spec.loader.exec_module(trace)
    result = trace.summarize([dict(name='GLES2::ReadPixels',ts=1),
                              dict(name='frame-video-playback-start',ts=10),
                              dict(name='D3D11VideoDecoder::Decode',ts=15),
                              dict(name='frame-video-playback-end',ts=20)])
    assert result['playbackReadbacks'] == {}
    assert result['diagnosticAndPlaybackReadbacks'] == {'GLES2::ReadPixels':1}
    with pytest.raises(ValueError,match='complete'):
        trace.summarize([])
