rnnoise_sources = [
  'src/rnn_vad_weights.cc',
]

librnnoise = static_library('librnnoise',
    rnnoise_sources,
    dependencies: common_deps,
    include_directories: webrtc_inc,
    cpp_args : common_cxxflags
)

rnnoise_dep = declare_dependency(
    link_with: librnnoise
)

