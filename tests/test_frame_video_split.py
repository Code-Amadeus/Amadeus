"""Separate VP9 color/alpha geometry, pairing, fidelity and source contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct

import numpy as np
import pytest
from PIL import Image

pytest.importorskip('cv2', reason='Optional offline video probe requires OpenCV')

spec = importlib.util.spec_from_file_location(
    'frame_video_split', Path(__file__).parents[1]/'tools/probes/frame-video/split_encode.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_split_padding_preserves_every_alpha_byte_and_visible_source_color():
    source = np.zeros((3, 257, 4), dtype=np.uint8)
    source[..., :3] = [160, 80, 200]
    source[:, :256, 3] = np.arange(256, dtype=np.uint8)
    original = source.copy()
    color, payload = module.split_frame(source)
    assert color.shape == (4, 258, 3)
    alpha = np.frombuffer(payload[:258*4], np.uint8).reshape(4, 258)
    np.testing.assert_array_equal(alpha[:3, :257], source[..., 3])
    np.testing.assert_array_equal(alpha[3], alpha[2])
    np.testing.assert_array_equal(alpha[:, 257], alpha[:, 256])
    assert payload[258*4:] == bytes([128])*(258*4//2)
    np.testing.assert_array_equal(color[:3, :257][source[..., 3] > 0], source[..., :3][source[..., 3] > 0])
    np.testing.assert_array_equal(source, original)


def test_split_fill_only_changes_fully_transparent_rgb():
    source = np.zeros((5, 7, 4), dtype=np.uint8)
    source[1:4, 1:6] = [140, 80, 30, 1]
    color, payload = module.split_frame(source)
    assert np.all(color[:5, :7][source[..., 3] == 0] == [140, 80, 30])
    assert np.frombuffer(payload, np.uint8)[0] == 0
    np.testing.assert_array_equal(color[1:4, 1:6], source[1:4, 1:6, :3])


def synthetic_ivf(timestamps=(0, 1), *, keys=('K_', '__')):
    header = bytearray(32)
    header[:4] = b'DKIF'
    header[8:12] = b'VP90'
    struct.pack_into('<HH', header, 4, 0, 32)
    struct.pack_into('<HH', header, 12, 8, 6)
    struct.pack_into('<I', header, 24, len(timestamps))
    data = bytes(header)+b''.join(struct.pack('<IQ', 3, i)+b'abc' for i in timestamps)
    packets = [dict(size='3', flags=key) for key in keys]
    return data, packets


def test_index_checks_frame_identity_geometry_and_common_recovery_keys():
    data, packets = synthetic_ivf()
    layout = module.source_layout(7, 5)
    index = module.validated_index(data, packets, 2, layout, [0])
    assert [entry['sourceIndex'] for entry in index] == [0, 1]
    with pytest.raises(ValueError, match='keyframes'):
        module.validated_index(data, packets, 2, layout, [0, 1])
    with pytest.raises(ValueError, match='geometry'):
        module.validated_index(data, packets, 2, module.source_layout(9, 5), [0])
    bad, packets = synthetic_ivf(timestamps=(0, 0))
    with pytest.raises(ValueError, match='timestamp/sourceIndex'):
        module.validated_index(bad, packets, 2, layout, [0])


def test_alpha_metrics_expose_one_byte_endpoint_errors_without_clipping():
    source = np.array([[0, 1, 128, 254, 255]], np.uint8)
    actual = np.array([[1, 2, 127, 254, 254]], np.uint8)
    totals = module.alpha_totals()
    item, previous = module.accumulate_alpha(totals, source, actual)
    measured = module.summarized_alpha(totals)
    assert item['zeroMax'] == 1
    assert measured['zeroLeaks'] == measured['opaqueLoss'] == 1
    assert measured['opaqueMin'] == 254
    assert not measured['exactAlphaEndpoints']
    assert measured['edgeAlphaMAE'] == pytest.approx(2/3)
    assert measured['edgeAlphaMaxAbsError'] == 1
    module.accumulate_alpha(totals, source, source, previous)
    assert module.summarized_alpha(totals)['alphaTemporalErrorMAE'] == pytest.approx(4/5)


def test_alpha_exact_ramp_reports_exact_all_pixels_and_endpoints():
    source = np.arange(256, dtype=np.uint8).reshape(1, 256)
    totals = module.alpha_totals()
    module.accumulate_alpha(totals, source, source)
    measured = module.summarized_alpha(totals)
    assert measured['exactAllAlpha'] and measured['exactAlphaEndpoints']
    assert measured['alphaMAE'] == measured['alphaMaxAbsError'] == 0
    assert measured['zeroPixels'] == measured['opaquePixels'] == 1


def test_color_metrics_weight_visible_and_edge_pixels_without_canvas_dilution():
    source = np.zeros((8, 8, 4), dtype=np.uint8)
    source[0, 0] = [100, 100, 100, 255]
    source[0, 1] = [100, 100, 100, 128]
    actual = source[..., :3].copy()
    actual[0, :2] += 10
    actual[1:, :] = 255  # Invisible RGB must not improve or worsen visible metrics.
    measured = module.summarized_color(module.color_metrics(source, actual, source[..., 3]))
    assert measured['visibleColorSamples'] == 6
    assert measured['edgeColorSamples'] == 3
    assert measured['visibleColorRMSE'] == measured['edgeColorRMSE'] == 10
    assert measured['alphaWeightedColorRMSE'] == pytest.approx(10)
    assert measured['compositeBlackRMSE'] < measured['visibleCompositeBlackRMSE']


def write_source(path, frames):
    path.mkdir()
    payload = frames.tobytes()
    (path/'source.rgba').write_bytes(payload)
    metadata = dict(frames=len(frames), height=frames.shape[1], width=frames.shape[2],
                    sourceSha256=hashlib.sha256(payload).hexdigest(), frameIntervalMs=17)
    (path/'source.json').write_text(json.dumps(metadata), encoding='utf-8')
    return metadata


def test_source_hash_is_checked_before_output_or_encoding(tmp_path):
    source = tmp_path/'source'
    metadata = write_source(source, np.zeros((3, 5, 7, 4), np.uint8))
    metadata['sourceSha256'] = '0'*64
    (source/'source.json').write_text(json.dumps(metadata), encoding='utf-8')
    output = tmp_path/'encoded'
    with pytest.raises(ValueError, match='hash'):
        module.export(source, output, head_frames=1)
    assert not output.exists()


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                    reason='Tiny software VP9 integration requires existing FFmpeg/ffprobe')
def test_tiny_dual_stream_roundtrip_exact_alpha_and_odd_frame_pairing(tmp_path):
    source = tmp_path/'source'
    frames = np.empty((4, 9, 257, 4), np.uint8)
    for i, frame in enumerate(frames):
        frame[..., :3] = [40+i*40, 80, 200]
        frame[:, :256, 3] = np.arange(256, dtype=np.uint8)
        frame[:, 256, 3] = 255
        frame[0, :, 3] = 0
        frame[1, :, 3] = 255
        frame[1:5, :32, :3] = i*60
        frame[1:5, :32, 3] = 255
        frame[5:7, 64:96, 3] = i*60  # Alpha identity catches stream/frame mispairing.
    metadata = write_source(source, frames)
    output = tmp_path/'encoded'
    report = module.export(source, output, head_frames=1, key_interval=2, threads=1)
    manifest = json.loads((output/'clip.json').read_text(encoding='utf-8'))
    assert manifest['sourceSha256'] == metadata['sourceSha256'] == module.sha256_file(source/'source.rgba')
    assert manifest['layout'] == dict(width=257, height=9, codedWidth=258, codedHeight=10)
    assert report['quality']['frames'] == 4
    assert report['quality']['decodedFrameCountsMatch']
    assert report['quality']['alphaLuma']['exactAllAlpha']
    assert report['quality']['alphaRgbRed']['exactAllAlpha']
    assert report['quality']['alphaRgbMaxChannelDifference'] == 0
    assert [item['sourceIndex'] for item in report['quality']['perFrame']] == list(range(4))
    assert report['bytes'] == sum(report['streamBytes'].values())
    for label in ('color', 'alpha'):
        stream = manifest['streams'][label]
        assert len(stream['index']) == 4
        assert [entry['sourceIndex'] for entry in stream['index']] == list(range(4))
        assert all(stream['index'][i]['key'] for i in (0, 1, 3))
        assert stream['videoSha256'] == module.sha256_file(output/stream['video'])
        assert stream['colorSpace']['fullRange'] == (label == 'alpha')
    assert (output/'decoded-000003.png').is_file()
    for i in range(4):
        decoded = np.asarray(Image.open(output/f'decoded-{i:06d}.png'))
        assert decoded.shape == (9, 257, 4)
        assert np.median(decoded[2:4, 8:24, :3]) == pytest.approx(i*60, abs=4)
    with pytest.raises(FileExistsError):
        module.export(source, output, head_frames=1, threads=1)
