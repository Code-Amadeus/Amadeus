system_wrappers_sources = [
  'source/cpu_features.cc',
  'source/denormal_disabler.cc',
  'source/field_trial.cc',
  'source/metrics.cc',
  'source/sleep.cc',
]

system_headers = [
  'include/cpu_features_wrapper.h',
  'include/metrics.h',
  'include/sleep.h',
]

install_headers(system_headers,
    subdir: join_paths(include_subdir, 'system_wrappers', 'include')
)

libsystem_wrappers = static_library('system_wrappers',
    system_wrappers_sources,
    dependencies: common_deps,
    include_directories: webrtc_inc,
    c_args : common_cflags,
    cpp_args : common_cxxflags
)

system_wrappers_dep = declare_dependency(
    link_with: libsystem_wrappers
)
