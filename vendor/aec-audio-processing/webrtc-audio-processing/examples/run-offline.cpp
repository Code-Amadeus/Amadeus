/*
 *  Copyright (c) 2024 Asymptotic Inc. All Rights Reserved.
 *  Author: Arun Raghavan <arun@asymptotic.io>
 *
 *  Use of this source code is governed by a BSD-style license
 *  that can be found in the LICENSE file in the root of the source
 *  tree. An additional intellectual property rights grant can be found
 *  in the file PATENTS.  All contributing project authors may
 *  be found in the AUTHORS file in the root of the source tree.
 */

#include "api/scoped_refptr.h"
#include <cstdlib>
#include <iostream>
#include <fstream>

#include <webrtc/modules/audio_processing/include/audio_processing.h>

#define DEFAULT_BLOCK_MS 10
#define DEFAULT_RATE 32000
#define DEFAULT_CHANNELS 1

int main(int argc, char **argv) {
    if (argc != 4) {
	std::cerr << "Usage: " << argv[0] << " <play_file> <rec_file> <out_file>" << std::endl;
	return EXIT_FAILURE;
    }

    std::ifstream play_file(argv[1], std::ios::binary);
    std::ifstream rec_file(argv[2], std::ios::binary);
    std::ofstream aec_file(argv[3], std::ios::binary);

    rtc::scoped_refptr<webrtc::AudioProcessing> apm = webrtc::AudioProcessingBuilder().Create();

    webrtc::AudioProcessing::Config config;
    config.echo_canceller.enabled = true;
    config.echo_canceller.mobile_mode = false;
    config.gain_controller1.enabled = true;
    config.gain_controller1.mode = webrtc::AudioProcessing::Config::GainController1::kAdaptiveAnalog;

    config.gain_controller2.enabled = true;

    config.high_pass_filter.enabled = true;

    apm->ApplyConfig(config);

    webrtc::StreamConfig stream_config(DEFAULT_RATE, DEFAULT_CHANNELS);

    while (!play_file.eof() && !rec_file.eof()) {
	int16_t play_frame[DEFAULT_RATE * DEFAULT_BLOCK_MS / 1000 * DEFAULT_CHANNELS];
	int16_t rec_frame[DEFAULT_RATE * DEFAULT_BLOCK_MS / 1000 * DEFAULT_CHANNELS];

	play_file.read(reinterpret_cast<char *>(play_frame), sizeof(play_frame));
	rec_file.read(reinterpret_cast<char *>(rec_frame), sizeof(rec_frame));

	apm->ProcessReverseStream(play_frame, stream_config, stream_config, play_frame);
	apm->ProcessStream(rec_frame, stream_config, stream_config, rec_frame);

	aec_file.write(reinterpret_cast<char *>(rec_frame), sizeof(rec_frame));
    }

    play_file.close();
    rec_file.close();
    aec_file.close();

    return EXIT_SUCCESS;
}
