base_sources = [
  'checks.cc',
  'containers/flat_tree.cc',
  'event.cc',
  'event_tracer.cc',
  'experiments/field_trial_parser.cc',
  'logging.cc',
  'memory/aligned_malloc.cc',
  'platform_thread.cc',
  'platform_thread_types.cc',
  'race_checker.cc',
  'random.cc',
  'string_encode.cc',
  'string_to_number.cc',
  'string_utils.cc',
  'strings/string_builder.cc',
  'synchronization/sequence_checker_internal.cc',
  'synchronization/yield_policy.cc',
  'system/file_wrapper.cc',
  'system_time.cc',
  'time_utils.cc',
  'zero_memory.cc',
]

base_headers = [
  [ '', 'arraysize.h' ],
  [ '', 'checks.h' ],
  [ '', 'ref_count.h' ],
  [ '', 'thread_annotations.h' ],
  [ '', 'type_traits.h' ],
  [ 'numerics', 'divide_round.h' ],
  [ 'numerics', 'safe_compare.h' ],
  [ 'numerics', 'safe_conversions.h' ],
  [ 'numerics', 'safe_conversions_impl.h' ],
  [ 'system', 'file_wrapper.h' ],
  [ 'system', 'inline.h' ],
  [ 'system', 'rtc_export.h' ],
  [ 'units', 'unit_base.h' ],
]

foreach h : base_headers
  install_headers(
    join_paths(h[0], h[1]),
    subdir: join_paths(include_subdir, 'rtc_base', h[0])
  )
endforeach

base_os_deps = []
if host_system == 'darwin'
  base_os_deps += dependency('appleframeworks', modules: [ 'Foundation' ])
elif host_system == 'ios'
  base_os_deps += dependency('appleframeworks',
    modules: [
      'CFNetwork',
      'Foundation',
      'Security',
      'SystemConfiguration',
      'UIKit',
    ]
  )
elif host_system == 'android'
  base_sources += [
    'system/warn_current_thread_is_deadlocked.cc',
  ]
endif

libbase = static_library('libbase',
    base_sources,
    dependencies: common_deps + base_os_deps,
    include_directories: webrtc_inc,
    cpp_args : common_cxxflags
)

base_dep = declare_dependency(
    link_with: libbase
)

