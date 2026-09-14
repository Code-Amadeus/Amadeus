#ifndef __AUDIO_PROCESSING_MODULE_H__
#define __AUDIO_PROCESSING_MODULE_H__

#include "webrtc/api/scoped_refptr.h"
#include "webrtc/api/audio/audio_processing.h"
#include "webrtc/common_audio/vad/include/vad.h"
#include <memory>

namespace webrtc {
    class AudioProcessing;
    class ProcessingConfig;
    class StreamConfig;
    template <typename T> class ChannelBuffer;
}

using namespace std;
using namespace webrtc;

class AudioProcessor {
public:
    /**
     * @brief Create an AudioProcessor instance.
     * Instantiated with default settings that can be changed later.
        * @param enable_aec Enable Acoustic Echo Cancellation (AEC).
        * @param enable_ns Enable Noise Suppression (NS).
        * @param ns_level Set Noise Suppression level (0=Low, 1=Moderate, 2=High, 3=VeryHigh).
        * @param enable_agc Enable Automatic Gain Control (AGC).
        * @param agc_mode Set Automatic Gain Control mode (0=AdaptiveAnalog, 1=AdaptiveDigital, 2=FixedDigital).
        * @param enable_vad Enable Voice Activity Detection (VAD).
     */
    // MODIFIED CONSTRUCTOR SIGNATURE
    AudioProcessor(bool enable_aec = true,
                   bool enable_ns = true,
                   int ns_level = 2,
                   bool enable_agc = true,
                   int agc_mode = 1,
                   bool enable_vad = true);
    
    /**
     * @brief Set the forward stream format.
     * This method must be called before processing audio.
        * @param sample_rate_in Input sample rate in Hz.
        * @param channel_count_in Number of input audio channels.
        * @param sample_rate_out Output sample rate in Hz.
        * @param channel_count_out Number of output audio channels.
     */
    void set_stream_format(int sample_rate_in = 32000,
                           int channel_count_in = 1,
                           int sample_rate_out = -1,
                           int channel_count_out = -1);

    /**
     * @brief Set the reverse stream format. The reverse stream is
     * typically used for echo cancellation.
     * This method must be called before processing audio.
        * @param sample_rate_in Reverse input sample rate in Hz.
        * @param channel_count_in Number of reverse input audio channels.
     */
    void set_reverse_stream_format(int sample_rate_in = 32000, 
                                   int channel_count_in = 1);

    /**
     * @brief Set the expected delay of the audio stream.
        * @param delay_ms Delay in milliseconds.
     */
    void set_stream_delay(int delay_ms);

    /**
     * @brief Set the VAD aggressiveness level.
        * @param aggressiveness VAD aggressiveness level (0-3).
     */
    void set_vad_aggressiveness(int aggressiveness);

    /**
     * @brief Get the sample rate of the input stream.
        * @return Sample rate in Hz.
     */
    int get_sample_rate_in() const;

    /**
     * @brief Get the sample rate of the output stream.
        * @return Sample rate in Hz.
     */
    int get_sample_rate_out() const;

    /**
     * @brief Get the number of channels in the input stream.
        * @return Number of channels.
     */
    int get_channel_count_in() const;

    /**
     * @brief Get the number of channels in the output stream.
        * @return Number of channels.
     */
    int get_channel_count_out() const;

    /**
     * @brief Get the sample rate of the reverse input stream.
        * @return Sample rate in Hz.
     */
    int get_reverse_sample_rate_in() const;

    /**
     * @brief Get the number of channels in the reverse input stream.
        * @return Number of channels.
     */
    int get_reverse_channel_count_in() const;

    /**
     * @brief Get the expected delay of the audio stream.
        * @return Delay in milliseconds.
     */
    int get_stream_delay() const;

    bool aec_enabled() const;
    bool ns_enabled() const;
    bool agc_enabled() const;
    bool vad_enabled() const;

    /**
     * @brief Process forard audio stream (i.e. from a microphone).
        * @param input Input audio data as a string of interleaved 16-bit samples.
     */
    std::string process_stream(const std::string& input);

    /**
     * @brief Process reverse audio stream (i.e. from speakers).
        * @param input Input audio data as a string of interleaved 16-bit samples.
     */
    std::string process_reverse_stream(const std::string& input);

    /**
     * @brief Check if voice was detected in the last processed frame.
        * @return True if voice activity was detected in the last frame, false otherwise.
     */
    bool has_voice();

    int get_frame_size() const;

    ~AudioProcessor();

private:
    webrtc::scoped_refptr<webrtc::AudioProcessing> apm_;
    std::unique_ptr<webrtc::StreamConfig> stream_config_in_;
    std::unique_ptr<webrtc::StreamConfig> stream_config_out_;
    std::unique_ptr<webrtc::StreamConfig> reverse_stream_config_in_;
    std::unique_ptr<webrtc::StreamConfig> reverse_stream_config_out_;
    webrtc::AudioProcessing::Config config_;
    
    // VAD components
    std::unique_ptr<webrtc::Vad> vad_;
    bool vad_enabled_;
    bool last_vad_activity_;
    int vad_aggressiveness_;
};


#endif // __AUDIO_PROCESSING_H__