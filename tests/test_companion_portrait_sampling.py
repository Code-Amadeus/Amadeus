"""Timing tests for the opt-in portrait experiment; no installed media required."""
from tools.probes.build_companion_portraits import matched_indices, timing_indices


def test_full_clip_preserves_duration_at_different_sampling_rates():
    for count, interval in [(120,17),(180,17),(300,17),(120,21)]:
        for fps in [12,24,30,60]:
            indices, duration = timing_indices(count, interval, fps)
            assert duration == count * interval
            assert indices == sorted(set(indices))
            assert indices[0] == 0 and indices[-1] < count
            for i, index in enumerate(indices):
                assert 0 <= i*duration/len(indices) - index*interval < interval + 1e-8


def test_densifying_old_segment_keeps_every_keyframe_onset_and_last_hold():
    anchors = [12,17,21,26,30,35]
    result = matched_indices(anchors)
    assert len(result) == 24
    assert result[::4] == anchors
    assert len(result)*42.5 == len(anchors)*170
    assert result[-4:] == [35]*4
    assert result == sorted(result)
