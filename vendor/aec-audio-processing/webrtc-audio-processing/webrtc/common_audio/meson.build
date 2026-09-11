common_audio_sources = [
  'audio_converter.cc',
  'audio_util.cc',
  'channel_buffer.cc',
  'fir_filter_c.cc',
  'fir_filter_factory.cc',
  'resampler/push_resampler.cc',
  'resampler/push_sinc_resampler.cc',
  'resampler/resampler.cc',
  'resampler/sinc_resampler.cc',
  'resampler/sinusoidal_linear_chirp_source.cc',
  'ring_buffer.c',
  'signal_processing/auto_correlation.c',
  'signal_processing/auto_corr_to_refl_coef.c',
  'signal_processing/complex_bit_reverse.c',
  'signal_processing/complex_fft.c',
  'signal_processing/copy_set_operations.c',
  'signal_processing/cross_correlation.c',
  'signal_processing/division_operations.c',
  'signal_processing/dot_product_with_scale.cc',
  'signal_processing/downsample_fast.c',
  'signal_processing/filter_ar.c',
  'signal_processing/filter_ar_fast_q12.c',
  'signal_processing/energy.c',
  'signal_processing/filter_ma_fast_q12.c',
  'signal_processing/get_hanning_window.c',
  'signal_processing/get_scaling_square.c',
  'signal_processing/ilbc_specific_functions.c',
  'signal_processing/levinson_durbin.c',
  'signal_processing/lpc_to_refl_coef.c',
  'signal_processing/min_max_operations.c',
  'signal_processing/randomization_functions.c',
  'signal_processing/real_fft.c',
  'signal_processing/refl_coef_to_lpc.c',
  'signal_processing/resample_48khz.c',
  'signal_processing/resample_by_2.c',
  'signal_processing/resample_by_2_internal.c',
  'signal_processing/resample.c',
  'signal_processing/resample_fractional.c',
  'signal_processing/spl_init.c',
  'signal_processing/spl_inl.c',
  'signal_processing/splitting_filter.c',
  'signal_processing/spl_sqrt.c',
  'signal_processing/sqrt_of_one_minus_x_squared.c',
  'signal_processing/vector_scaling_operations.c',
  'smoothing_filter.cc',
  'third_party/ooura/fft_size_128/ooura_fft.cc',
  'third_party/ooura/fft_size_256/fft4g.cc',
  'third_party/spl_sqrt_floor/spl_sqrt_floor.c',
  'vad/vad.cc',
  'vad/vad_core.c',
  'vad/vad_filterbank.c',
  'vad/vad_gmm.c',
  'vad/vad_sp.c',
  'vad/webrtc_vad.c',
]

arch_libs = []
if have_x86
  arch_libs += [
    static_library('common_audio_sse2',
      [
        'fir_filter_sse.cc',
        'resampler/sinc_resampler_sse.cc',
        'third_party/ooura/fft_size_128/ooura_fft_sse2.cc',
      ],
      dependencies: common_deps,
      include_directories: webrtc_inc,
      c_args: common_cflags + ['-msse2'],
      cpp_args: common_cxxflags + ['-msse2']
    )
  ]
  arch_libs += [
    static_library('common_audio_avx',
      [
        'fir_filter_avx2.cc',
        'resampler/sinc_resampler_avx2.cc',
      ],
      dependencies: common_deps,
      include_directories: webrtc_inc,
      c_args: common_cflags + avx_flags,
      cpp_args: common_cxxflags + avx_flags
    )
  ]
endif

if have_mips
  common_audio_sources += [
    'signal_processing/complex_bit_reverse_mips.c',
    'signal_processing/complex_fft_mips.c',
    'signal_processing/cross_correlation_mips.c',
    'signal_processing/downsample_fast_mips.c',
    'signal_processing/filter_ar_fast_q12_mips.c',
    'signal_processing/min_max_operations_mips.c',
    'signal_processing/resample_by_2_mips.c',
    'signal_processing/vector_scaling_operations_mips.c',
    'third_party/ooura/fft_size_128/ooura_fft_mips.cc',
    'third_party/spl_sqrt_floor/spl_sqrt_floor_mips.c',
  ]
endif

if have_arm
  common_audio_sources += [
    'signal_processing/complex_bit_reverse_arm.S',
    'third_party/spl_sqrt_floor/spl_sqrt_floor_arm.S',
  ]
endif

if have_armv7
  common_audio_sources += [
    'signal_processing/filter_ar_fast_q12_armv7.S',
  ]
endif

if neon_opt.enabled()
  common_audio_sources += [
    'fir_filter_neon.cc',
    'resampler/sinc_resampler_neon.cc',
    'signal_processing/cross_correlation_neon.c',
    'signal_processing/downsample_fast_neon.c',
    'signal_processing/min_max_operations_neon.c',
    'third_party/ooura/fft_size_128/ooura_fft_neon.cc',
  ]
endif

if not have_arm
  common_audio_sources += [
    'signal_processing/complex_bit_reverse.c',
    'signal_processing/filter_ar_fast_q12.c',
  ]
endif

libcommon_audio = static_library('common_audio',
    common_audio_sources,
    dependencies: common_deps,
    include_directories: webrtc_inc,
    c_args: common_cflags,
    cpp_args: common_cxxflags
)

common_audio_dep = declare_dependency(
  link_with: [libcommon_audio] + arch_libs,
)
