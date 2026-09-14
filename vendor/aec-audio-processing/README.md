# AEC Audio Processing Module

A Python module for real-time audio processing using WebRTC technology, optimized and maintained by TheDeveloper. This package provides an easy-to-use interface for audio processing capabilities including echo cancellation, noise suppression, and automatic gain control.

## Features

- **Acoustic Echo Cancellation (AEC)**: Removes echo from audio streams
- **Noise Suppression (NS)**: Reduces background noise
- **Automatic Gain Control (AGC)**: Automatically adjusts audio levels

## Requirements
+ swig
+ meson
+ compile toolchain
+ python3

## Installation

You can install the package directly from PyPI:

```bash
pip install aec-audio-processing
```

## Basic Usage

### Simple Audio Processing
```python
from aec_audio_processing  import AudioProcessor

# Initialize with all features enabled
ap = AudioProcessor(enable_aec=True, enable_ns=True, enable_agc=True)
ap.set_stream_format(16000, 1)      # 16kHz mono

# Process 10ms of audio data
audio_10ms = b'\0' * 160 * 2        # 10ms, 16000 sample rate, 16 bits, 1 channel
audio_out = ap.process_stream(audio_10ms)

# Check if voice was detected
if ap.has_voice():
    print("Voice detected!")
```

### Configuration Options

```python
from aec_audio_processing  import AudioProcessor

# Initialize with specific features
ap = AudioProcessor(
    enable_aec=True,    # Echo cancellation
    enable_ns=True,     # Noise suppression  
    enable_agc=True,    # Automatic gain control
    enable_vad=True     # Voice activity detection
)

# Set audio format
ap.set_stream_format(
    sample_rate_in=16000,      # Input sample rate (Hz)
    channel_count_in=1,        # Input channels
    sample_rate_out=16000,     # Output sample rate (Hz) 
    channel_count_out=1        # Output channels
)

# Set reverse stream for echo cancellation
ap.set_reverse_stream_format(16000, 1)

# Set stream delay for echo cancellation
ap.set_stream_delay(50)  # 50ms delay


# Check feature status
print(f"AEC enabled: {ap.aec_enabled()}")
print(f"NS enabled: {ap.ns_enabled()}")
print(f"AGC enabled: {ap.agc_enabled()}")
```