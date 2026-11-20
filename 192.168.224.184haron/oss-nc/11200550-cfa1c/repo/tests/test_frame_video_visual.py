"""Review evidence retains faint alpha, native crop geometry and input identity."""
import importlib.util
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

spec = importlib.util.spec_from_file_location(
    'frame_video_visual', Path(__file__).parents[1]/'tools/probes/frame-video/visual_review.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_compositing_retains_one_byte_background_leak_and_exact_endpoints():
    rgba = np.array([[[0, 0, 0, 0], [0, 0, 0, 1], [20, 30, 40, 255]]], np.uint8)
    original = rgba.copy()
    white = module.background_pixels(3, 1, module.BACKGROUNDS[1])
    result = module.composite(rgba, white)
    np.testing.assert_array_equal(result, [[[255, 255, 255], [254, 254, 254], [20, 30, 40]]])
    np.testing.assert_array_equal(rgba, original)


def test_native_crop_checker_phase_and_pixels_remain_canvas_anchored():
    reference = np.zeros((35, 37, 4), np.uint8)
    actual = reference.copy()
    actual[17, 17] = [0, 0, 0, 1]
    sheet, metadata = module.contact_sheet(reference, actual, source_index=7,
                                            view='crop', actual_label='Decoded',
                                            box=(15, 15, 5, 5), preview_max=1)
    assert metadata['nativeScale']
    assert metadata['scaleX'] == metadata['scaleY'] == 1
    checker_row = metadata['rows'][-1]
    pixels = np.asarray(sheet)
    tile = pixels[checker_row['top']:checker_row['top']+5,
                  checker_row['leftReference']:checker_row['leftReference']+5]
    expected = module.background_pixels(37, 35, module.BACKGROUNDS[-1])[15:20, 15:20]
    np.testing.assert_array_equal(tile, expected)


def test_automatic_crops_use_every_nonzero_source_alpha_and_fit_odd_canvas():
    alpha = np.zeros((31, 37), np.uint8)
    alpha[3:27, 9:30] = 255
    alpha[1, 12] = 1  # No minimum opacity cutoff may hide this reference region.
    head = module.crop_box(alpha, 'head', 12)
    assert head[1] == 1
    edge = module.crop_box(alpha, 'edge', 12)
    assert edge[0] < 9 < edge[0]+edge[2]
    module.validate_crop(head, 37, 31)
    module.validate_crop(edge, 37, 31)
    assert module.crop_box(alpha, 'head', 100) == (0, 0, 37, 31)


def test_review_sheets_record_exact_pairs_backgrounds_and_preserve_sources(tmp_path):
    fixture, captures = tmp_path/'fixture', tmp_path/'gpu'
    fixture.mkdir()
    captures.mkdir()
    reference = np.zeros((9, 17, 4), np.uint8)
    reference[2:7, 4:13] = [150, 60, 200, 255]
    actual = reference.copy()
    actual[1, 1] = [200, 200, 200, 1]
    reference_path, actual_path = fixture/'reference-000003.png', captures/'hardware-000003.png'
    Image.fromarray(reference).save(reference_path)
    Image.fromarray(actual).save(actual_path)
    hashes = module._hash(reference_path), module._hash(actual_path)
    output = tmp_path/'review'
    report = module.create_review(fixture, output, captures=captures, crop_size=5, preview_max=8,
                                  extra_crops=[(0, 0, 4, 4)])
    assert report['decodedKind'] == 'hardware-rgba-capture'
    assert report['frames'][0]['sourceIndex'] == 3
    assert [item['name'] for item in report['backgrounds']] == ['black', 'white', 'green', 'magenta', 'checker']
    assert len(report['artifacts']) == 4
    assert all(artifact['scaleX'] == artifact['scaleY'] == 1 for artifact in report['artifacts'][1:])
    assert report['artifacts'][0]['scaleX'] < 1
    saved = json.loads((output/'review.json').read_text(encoding='utf-8'))
    assert saved == report
    assert saved['frames'][0]['reference']['sha256'] == hashes[0]
    assert saved['frames'][0]['decoded']['sha256'] == hashes[1]
    assert (module._hash(reference_path), module._hash(actual_path)) == hashes
    with pytest.raises(FileExistsError):
        module.create_review(fixture, output, captures=captures)


def test_review_fails_when_pair_or_display_geometry_is_missing(tmp_path):
    fixture = tmp_path/'fixture'
    fixture.mkdir()
    Image.fromarray(np.zeros((3, 5, 4), np.uint8)).save(fixture/'decoded-000000.png')
    with pytest.raises(ValueError, match='complete'):
        module.create_review(fixture, tmp_path/'missing')
    Image.fromarray(np.zeros((3, 7, 4), np.uint8)).save(fixture/'reference-000000.png')
    with pytest.raises(ValueError, match='geometry'):
        module.create_review(fixture, tmp_path/'geometry')
