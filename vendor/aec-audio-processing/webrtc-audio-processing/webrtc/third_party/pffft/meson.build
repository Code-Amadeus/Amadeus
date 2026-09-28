pffft_sources = [
  'src/pffft.c',
]

pffft_cflags = [ '-D_GNU_SOURCE' ]

if (have_x86 and not have_inline_sse) or (have_arm and not neon_opt.enabled()) or (have_mips and host_machine.endian() == 'little') or have_mips64
  pffft_cflags += [ '-DPFFFT_SIMD_DISABLE' ]
endif

libpffft = static_library('libpffft',
    pffft_sources,
    dependencies: common_deps,
    include_directories: webrtc_inc,
    c_args : common_cflags + pffft_cflags
)

pffft_dep = declare_dependency(
    link_with: libpffft
)

