api_sources = [
  'audio/audio_frame.cc',
  'audio/audio_processing.cc',
  'audio/audio_processing_statistics.cc',
  'audio/channel_layout.cc',
  'audio/echo_canceller3_config.cc',
  'rtp_headers.cc',
  'rtp_packet_info.cc',
  'task_queue/task_queue_base.cc',
  'units/frequency.cc',
  'units/time_delta.cc',
  'units/timestamp.cc',
  'video/color_space.cc',
  'video/hdr_metadata.cc',
  'video/video_content_type.cc',
  'video/video_timing.cc',
]

api_headers = [
  ['', 'array_view.h'],
  ['', 'location.h'],
  ['', 'ref_count.h'],
  ['', 'scoped_refptr.h'],
  ['audio', 'audio_processing.h'],
  ['audio', 'audio_processing_statistics.h'],
  ['audio', 'echo_canceller3_config.h'],
  ['audio', 'echo_control.h'],
  ['task_queue', 'task_queue_base.h'],
  ['units', 'time_delta.h'],
]

foreach h : api_headers
  install_headers(
    join_paths(h[0], h[1]),
    subdir: join_paths(include_subdir, 'api', h[0])
  )
endforeach


libapi = static_library('libapi',
    api_sources,
    dependencies: common_deps,
    include_directories: webrtc_inc,
    cpp_args : common_cxxflags
)

api_dep = declare_dependency(
    link_with: libapi
)

