from webrtc_audio_processing import AudioProcessor as AP
import pytest
import struct
import math

def create_sine_wave(frequency, sample_rate, duration_ms, amplitude=0.5):
    """Create a sine wave audio signal."""
    num_samples = int(sample_rate * duration_ms / 1000)
    audio_data = bytearray()
    
    for i in range(num_samples):
        t = i / sample_rate
        sample = int(amplitude * 32767 * math.sin(2 * math.pi * frequency * t))
        audio_data.extend(struct.pack('<h', sample))
    
    return bytes(audio_data)

def create_echo_signal(original_signal, delay_samples, attenuation=0.3):
    """Create an echo signal by delaying and attenuating the original."""
    # Pad with zeros to create delay
    delayed_signal = b'\x00\x00' * delay_samples + original_signal
    
    # Attenuate the delayed signal
    attenuated_data = bytearray()
    for i in range(0, len(delayed_signal), 2):
        sample = struct.unpack('<h', delayed_signal[i:i+2])[0]
        attenuated_sample = int(sample * attenuation)
        attenuated_data.extend(struct.pack('<h', attenuated_sample))
    
    return bytes(attenuated_data)

def test_aec_echo_cancellation_validation():
    """Test that AEC actually cancels echo."""
    ap = AP(enable_aec=True, enable_ns=False, enable_agc=False)
    ap.set_stream_format(48000, 1)
    ap.set_reverse_stream_format(48000, 1)
    ap.set_stream_delay(50)  # 50ms delay
    
    # Create original signal (speaker output)
    original_signal = create_sine_wave(440, 48000, 10)  # 440Hz for 10ms
    
    # Create echo signal (delayed and attenuated version of original)
    echo_delay_samples = int(48000 * 0.05)  # 50ms delay
    echo_signal = create_echo_signal(original_signal, echo_delay_samples, 0.3)
    
    # Create microphone input (original + echo)
    mic_input = bytearray()
    for i in range(0, len(original_signal), 2):
        if i < len(echo_signal):
            sample1 = struct.unpack('<h', original_signal[i:i+2])[0]
            sample2 = struct.unpack('<h', echo_signal[i:i+2])[0]
            combined_sample = sample1 + sample2
            # Clamp to 16-bit range
            combined_sample = max(-32768, min(32767, combined_sample))
            mic_input.extend(struct.pack('<h', combined_sample))
        else:
            mic_input.extend(original_signal[i:i+2])
    
    mic_input = bytes(mic_input)
    
    # Process reverse stream first (speaker output)
    ap.process_reverse_stream(original_signal)
    
    # Process forward stream (microphone input with echo)
    output_data = ap.process_stream(mic_input)
    
    assert output_data is not None, "AEC processed output should not be None"
    assert len(output_data) == len(mic_input), "Output length should match input"
    
    # Calculate power of input vs output to check if echo was reduced
    input_power = sum(struct.unpack('<h', mic_input[i:i+2])[0]**2 
                     for i in range(0, len(mic_input), 2))
    output_power = sum(struct.unpack('<h', output_data[i:i+2])[0]**2 
                      for i in range(0, len(output_data), 2))
    
    # AEC should reduce the power (echo cancellation)
    # Note: This is a basic test - in practice, you'd want more sophisticated metrics
    assert output_power < input_power, "AEC should reduce signal power"

def test_aec_multiple_frames_adaptation():
    """Test AEC adaptation over multiple frames."""
    ap = AP(enable_aec=True, enable_ns=False, enable_agc=False)
    ap.set_stream_format(48000, 1)
    ap.set_reverse_stream_format(48000, 1)
    ap.set_stream_delay(50)
    
    # Create a longer signal for adaptation testing
    original_signal = create_sine_wave(440, 48000, 100)  # 100ms
    
    # Split into 10ms frames
    frame_size = ap.get_frame_size()
    num_frames = len(original_signal) // (frame_size * 2)  # 2 bytes per sample
    
    input_powers = []
    output_powers = []
    
    for i in range(num_frames):
        start_idx = i * frame_size * 2
        end_idx = start_idx + frame_size * 2
        
        # Get frame of original signal
        frame_original = original_signal[start_idx:end_idx]
        
        # Create echo for this frame
        echo_delay_samples = int(48000 * 0.05)  # 50ms delay
        frame_echo = create_echo_signal(frame_original, echo_delay_samples, 0.3)
        
        # Create microphone input (original + echo)
        frame_mic_input = bytearray()
        for j in range(0, len(frame_original), 2):
            if j < len(frame_echo):
                sample1 = struct.unpack('<h', frame_original[j:j+2])[0]
                sample2 = struct.unpack('<h', frame_echo[j:j+2])[0]
                combined_sample = sample1 + sample2
                combined_sample = max(-32768, min(32767, combined_sample))
                frame_mic_input.extend(struct.pack('<h', combined_sample))
            else:
                frame_mic_input.extend(frame_original[j:j+2])
        
        frame_mic_input = bytes(frame_mic_input)
        
        # Process reverse stream first
        ap.process_reverse_stream(frame_original)
        
        # Process forward stream
        frame_output = ap.process_stream(frame_mic_input)
        
        # Calculate powers
        input_power = sum(struct.unpack('<h', frame_mic_input[k:k+2])[0]**2 
                         for k in range(0, len(frame_mic_input), 2))
        output_power = sum(struct.unpack('<h', frame_output[k:k+2])[0]**2 
                          for k in range(0, len(frame_output), 2))
        
        input_powers.append(input_power)
        output_powers.append(output_power)
    
    # Check that AEC is working (output power should be less than input power)
    # and that it's adapting (later frames should show better cancellation)
    assert len(output_powers) > 0, "Should have processed multiple frames"
    assert all(op < ip for op, ip in zip(output_powers, input_powers)), \
        "AEC should reduce power in all frames"
    
    # Check that later frames show better cancellation (adaptation)
    if len(output_powers) > 3:
        early_avg = sum(output_powers[:3]) / 3
        late_avg = sum(output_powers[-3:]) / 3
        # Later frames should have lower power (better cancellation)
        assert late_avg <= early_avg, "AEC should adapt and improve over time"

def test_aec_silence_processing():
    """Test AEC with silent input."""
    ap = AP(enable_aec=True, enable_ns=False, enable_agc=False)
    ap.set_stream_format(48000, 1)
    ap.set_reverse_stream_format(48000, 1)
    
    # Create silence data (all zeros)
    frame_size = ap.get_frame_size()
    silence_data = b'\x00\x00' * frame_size  # 16-bit samples
    
    # Process silence
    output_data = ap.process_stream(silence_data)
    
    assert output_data is not None, "AEC should handle silence input"
    assert len(output_data) == len(silence_data), "Silence output length should match input"

def test_aec_error_handling():
    """Test AEC error handling."""
    ap = AP(enable_aec=True, enable_ns=False, enable_agc=False)
    ap.set_stream_format(48000, 1)
    ap.set_reverse_stream_format(48000, 1)
    
    # Test with empty data
    with pytest.raises(Exception):
        ap.process_stream("")
    
    # Test with invalid data length
    invalid_data = b'\x00\x00'  # Too short for 10ms at 48kHz
    with pytest.raises(Exception):
        ap.process_stream(invalid_data)