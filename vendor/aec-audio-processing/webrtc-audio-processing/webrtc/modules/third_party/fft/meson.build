fft_sources = ['fft.c']

libfft = static_library('libfft',
    fft_sources,
    dependencies: common_deps,
    include_directories: webrtc_inc,
    cpp_args : common_cxxflags
)

fft_dep = declare_dependency(
    link_with: libfft
)

