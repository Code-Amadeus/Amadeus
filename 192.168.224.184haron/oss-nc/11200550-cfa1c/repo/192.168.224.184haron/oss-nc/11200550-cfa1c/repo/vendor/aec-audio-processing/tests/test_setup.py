from webrtc_audio_processing import AudioProcessor as AP
import pytest

def test_constructor():
    ap = AP(enable_aec=True, enable_ns=True, enable_agc=True)
    assert ap is not None, "AudioProcessor instance should not be None"
    assert ap.aec_enabled(), "AEC should be enabled"
    assert ap.ns_enabled(), "NS should be enabled"
    assert ap.agc_enabled(), "AGC should be enabled"

    ap = AP(enable_aec=False, enable_ns=False, enable_agc=False)
    assert ap is not None, "AudioProcessor instance should not be None"
    assert not ap.aec_enabled(), "AEC should be disabled"
    assert not ap.ns_enabled(), "NS should be disabled"
    assert not ap.agc_enabled(), "AGC should be disabled"

def test_set_stream_format():
    ap = AP(enable_aec=True, enable_ns=True, enable_agc=True)

    ap.set_stream_format(48000, 1)
    assert ap.get_sample_rate_in() == 48000, "Sample rate should be set to 48000"
    assert ap.get_channel_count_in() == 1, "Number of channels should be set to 1"
    assert ap.get_sample_rate_out() == 48000, "Output sample rate should match input sample rate"
    assert ap.get_channel_count_out() == 1, "Output channel count should match input channel count"

    ap.set_stream_format(32000, 2, 30000, 1)
    assert ap.get_sample_rate_in() == 32000, "Sample rate should be set to 32000"
    assert ap.get_channel_count_in() == 2, "Number of channels should be set to 2"
    assert ap.get_sample_rate_out() == 30000, "Output sample rate should be set to 30000"
    assert ap.get_channel_count_out() == 1, "Output channel count should be set to 1"
    
    with pytest.raises(ValueError):
        ap.set_stream_format(0, 1), "Setting sample rate to 0 should raise ValueError"
    with pytest.raises(ValueError):
        ap.set_stream_format(48000, 0), "Setting channel count to 0 should raise ValueError"


def test_set_reverse_stream_format():
    ap = AP(enable_aec=True, enable_ns=True, enable_agc=True)
    
    ap.set_reverse_stream_format(48000, 1)
    assert ap.get_reverse_sample_rate_in() == 48000, "Reverse sample rate should be set to 48000"
    assert ap.get_reverse_channel_count_in() == 1, "Reverse channel count should be set to 1"
    
    with pytest.raises(ValueError):
        ap.set_reverse_stream_format(0, 1), "Setting reverse sample rate to 0 should raise ValueError"
    with pytest.raises(ValueError):
        ap.set_reverse_stream_format(48000, 0), "Setting reverse channel count to 0 should raise ValueError"

def test_stream_delay():
    ap = AP(enable_aec=True, enable_ns=True, enable_agc=True)
    ap.set_stream_delay(50)
    assert ap.get_stream_delay() == 50, "Stream delay should be set to 50 ms"

    with pytest.raises(ValueError):
        ap.set_stream_delay(-10), "Setting negative stream delay should raise ValueError"

def test_frame_size():
    ap = AP(enable_aec=True, enable_ns=True, enable_agc=True)
    ap.set_stream_format(48000, 1)

    frame_size = ap.get_frame_size()
    assert frame_size > 0, "Frame size should be greater than 0"
    assert frame_size == 480, "Frame size should be 480 for 48000 Hz sample rate and 10 ms frame duration"