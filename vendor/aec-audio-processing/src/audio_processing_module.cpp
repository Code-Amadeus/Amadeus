#include "audio_processing_module.h"

#include "webrtc/api/audio/audio_processing.h"
#include "webrtc/api/scoped_refptr.h"
#include "webrtc/common_audio/vad/include/vad.h"
#include <cstdint>
#include <iostream>
using namespace std;


AudioProcessor::AudioProcessor(bool enable_aec,
                               bool enable_ns,
                               int ns_level,
                               bool enable_agc,
                               int agc_mode,
                               bool enable_vad) { 
    apm_ = webrtc::AudioProcessingBuilder().Create();

    // initialize echo cancelling
    config_.echo_canceller.enabled = enable_aec;
    config_.echo_canceller.mobile_mode = false;

    
    config_.noise_suppression.enabled = enable_ns;
    switch (ns_level) {
        case 0:
            config_.noise_suppression.level = webrtc::AudioProcessing::Config::NoiseSuppression::Level::kLow;
            break;
        case 1:
            config_.noise_suppression.level = webrtc::AudioProcessing::Config::NoiseSuppression::Level::kModerate;
            break;
        case 3:
            config_.noise_suppression.level = webrtc::AudioProcessing::Config::NoiseSuppression::Level::kVeryHigh;
            break;
        case 2: // Default
        default:
            config_.noise_suppression.level = webrtc::AudioProcessing::Config::NoiseSuppression::Level::kHigh;
            break;
    }

    
    config_.gain_controller1.enabled = enable_agc;
    switch (agc_mode) {
        case 0: // kAdaptiveAnalog
            config_.gain_controller1.mode = webrtc::AudioProcessing::Config::GainController1::Mode::kAdaptiveAnalog;
            break;
        case 2: // kFixedDigital
            config_.gain_controller1.mode = webrtc::AudioProcessing::Config::GainController1::Mode::kFixedDigital;
            break;
        case 1: 
        default:
            config_.gain_controller1.mode = webrtc::AudioProcessing::Config::GainController1::Mode::kAdaptiveDigital;
            break;
    }
    
    apm_->ApplyConfig(config_);

    stream_config_in_ = std::make_unique<webrtc::StreamConfig>();
    stream_config_out_ = std::make_unique<webrtc::StreamConfig>();
    reverse_stream_config_in_ = std::make_unique<webrtc::StreamConfig>();
    
    // initialize VAD
    // vad_enabled_ = enable_vad;
    vad_aggressiveness_ = webrtc::Vad::kVadNormal;
    last_vad_activity_ = false;
    
    // if (vad_enabled_) {
    //     vad_ = webrtc::CreateVad(static_cast<webrtc::Vad::Aggressiveness>(vad_aggressiveness_));
    // }
}

void AudioProcessor::set_stream_format(int sample_rate_in, 
                                       int channel_count_in, 
                                       int sample_rate_out,
                                       int channel_count_out) {

    if (sample_rate_in < 8000 || sample_rate_in > 384000) {
        throw std::invalid_argument("Sample rate must be between 8000 and 384000 Hz.");
    }
    if (channel_count_in < 1) {
        throw std::invalid_argument("Number of channels must be at least 1.");
    }

    stream_config_in_ = std::make_unique<webrtc::StreamConfig>(sample_rate_in, channel_count_in);
    // default to input sample rate and channel count if not specified
    if (sample_rate_out == -1) {
        sample_rate_out = sample_rate_in;
    }
    if (channel_count_out == -1) {
        channel_count_out = channel_count_in;
    }
    stream_config_out_ = std::make_unique<webrtc::StreamConfig>(sample_rate_out, channel_count_out);
}

void AudioProcessor::set_reverse_stream_format(int sample_rate_in,
                                               int channel_count_in) {

    if (sample_rate_in < 8000 || sample_rate_in > 384000) {
        throw std::invalid_argument("Sample rate must be between 8000 and 384000 Hz.");
    }
    if (channel_count_in < 1) {
        throw std::invalid_argument("Number of channels must be at least 1.");
    }

    reverse_stream_config_in_ = std::make_unique<webrtc::StreamConfig>(sample_rate_in, channel_count_in);
}

void AudioProcessor::set_stream_delay(int delay_ms) {
    if (delay_ms < 0) {
        throw std::invalid_argument("Delay must be greater than 0 ms.");
    }
    apm_->set_stream_delay_ms(delay_ms);
}

void AudioProcessor::set_vad_aggressiveness(int aggressiveness) {
    if (aggressiveness < 0 || aggressiveness > 3) {
        throw std::invalid_argument("VAD aggressiveness must be between 0 and 3.");
    }
    
    vad_aggressiveness_ = aggressiveness;
    
    if (vad_enabled_ && vad_) {
        vad_->Reset();
    }
}

int AudioProcessor::get_sample_rate_in() const {
    if (stream_config_in_) {
        return stream_config_in_->sample_rate_hz();
    } else {
        throw std::runtime_error("Stream format not set. Call set_stream_format() first.");
    }
}

int AudioProcessor::get_channel_count_in() const {
    if (stream_config_in_) {
        return stream_config_in_->num_channels();
    } else {
        throw std::runtime_error("Stream format not set. Call set_stream_format() first.");
    }
}

int AudioProcessor::get_sample_rate_out() const {
    if (stream_config_out_) {
        return stream_config_out_->sample_rate_hz();
    } else {
        throw std::runtime_error("Stream format not set. Call set_stream_format() first.");
    }
}

int AudioProcessor::get_channel_count_out() const {
    if (stream_config_out_) {
        return stream_config_out_->num_channels();
    } else {
        throw std::runtime_error("Stream format not set. Call set_stream_format() first.");
    }
}

int AudioProcessor::get_reverse_sample_rate_in() const {
    if (reverse_stream_config_in_) {
        return reverse_stream_config_in_->sample_rate_hz();
    } else {
        throw std::runtime_error("Reverse stream format not set. Call set_reverse_stream_format() first.");
    }
}

int AudioProcessor::get_reverse_channel_count_in() const {
    if (reverse_stream_config_in_) {
        return reverse_stream_config_in_->num_channels();
    } else {
        throw std::runtime_error("Reverse stream format not set. Call set_reverse_stream_format() first.");
    }
}

int AudioProcessor::get_stream_delay() const {
    return apm_->stream_delay_ms();
}

bool AudioProcessor::aec_enabled() const {
    return config_.echo_canceller.enabled;
}

bool AudioProcessor::ns_enabled() const {
    return config_.noise_suppression.enabled;
}

bool AudioProcessor::agc_enabled() const {
    return config_.gain_controller1.enabled;
}

bool AudioProcessor::vad_enabled() const {
    return vad_enabled_;
}

std::string AudioProcessor::process_stream(const std::string& input) {
    if (!stream_config_in_ || !stream_config_out_) {
        throw std::runtime_error("Stream format not set. Call set_stream_format() first.");
    }
    if (input.empty()) {
        return std::string();
    }

    // calculate expected size
    int frame_size = get_frame_size();
    int total_samples = frame_size * stream_config_in_->num_channels();
    int expected_size = total_samples * sizeof(int16_t);

    if (input.size() != expected_size) {
        throw std::invalid_argument("Input size does not match the expected frame size.");
    }

    // convert string to raw data
    const int16_t* input_data = reinterpret_cast<const int16_t*>(input.data());

    // perform VAD if enabled
    if (vad_enabled_ && vad_) {
        // For VAD, we only process the first channel if multi-channel
        int vad_samples = frame_size;
        if (stream_config_in_->num_channels() > 1) {
            // Extract first channel for VAD processing
            std::vector<int16_t> mono_data(frame_size);
            for (int i = 0; i < frame_size; ++i) {
                mono_data[i] = input_data[i * stream_config_in_->num_channels()];
            }
            webrtc::Vad::Activity activity = vad_->VoiceActivity(
                mono_data.data(), 
                static_cast<size_t>(vad_samples), 
                stream_config_in_->sample_rate_hz()
            );
            last_vad_activity_ = (activity == webrtc::Vad::kActive);
        } else {
            webrtc::Vad::Activity activity = vad_->VoiceActivity(
                input_data, 
                static_cast<size_t>(vad_samples), 
                stream_config_in_->sample_rate_hz()
            );
            last_vad_activity_ = (activity == webrtc::Vad::kActive);
        }
    }

    // allocate output
    std::string output(expected_size, '\0');
    int16_t* output_data = reinterpret_cast<int16_t*>(&output[0]);

    // process audio
    int result = apm_->ProcessStream(input_data, *stream_config_in_, *stream_config_out_, output_data);

    if (result != webrtc::AudioProcessing::kNoError) {
        throw std::runtime_error("Error processing audio stream: " + std::to_string(result));
    }

    return output;
}

std::string AudioProcessor::process_reverse_stream(const std::string& input) {
    if (!reverse_stream_config_in_) {
        throw std::runtime_error("Reverse stream format not set. Call set_reverse_stream_format() first.");
    }
    if (input.empty()) {
        return std::string();
    }

    // calculate expected size
    int frame_size = get_frame_size();
    int total_samples = frame_size * reverse_stream_config_in_->num_channels();
    int expected_size = total_samples * sizeof(int16_t);
    if (input.size() != expected_size) {
        throw std::invalid_argument("Input size does not match the expected frame size.");
    }
    
    // convert string to raw data
    const int16_t* input_data = reinterpret_cast<const int16_t*>(input.data());
    
    // allocate output
    std::string output(expected_size, '\0');
    int16_t* output_data = reinterpret_cast<int16_t*>(&output[0]);
    
    // process reverse audio
    int result = apm_->ProcessReverseStream(input_data, *reverse_stream_config_in_, *stream_config_out_, output_data);
    if (result != webrtc::AudioProcessing::kNoError) {
        throw std::runtime_error("Error processing reverse audio stream: " + std::to_string(result));
    }
    
    return output;
}

int AudioProcessor::get_frame_size() const {
    if (!stream_config_in_) {
        throw std::runtime_error("Stream format not set. Call set_stream_format() first.");
    }
    return webrtc::AudioProcessing::GetFrameSize(stream_config_in_->sample_rate_hz());
}

bool AudioProcessor::has_voice() {
    if (!vad_enabled_) {
        throw std::runtime_error("VAD is not enabled. Call set_vad_enabled() first.");
    }
    return last_vad_activity_;
}

AudioProcessor::~AudioProcessor() {
}
