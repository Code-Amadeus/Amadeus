#!/usr/bin/env python
# encoding: utf-8
"""
Python bindings of webrtc audio processing
"""

import os
import shutil
import subprocess
import sys
import glob
from setuptools import setup, Extension, find_packages
from setuptools.command.build_ext import build_ext as _build_ext


def get_webrtc_library_path():
    """Returns the path to the pre-compiled WebRTC library."""
    webrtc_dir = os.path.join(os.path.dirname(__file__), 'webrtc-audio-processing')
    install_dir = os.path.join(webrtc_dir, 'install')
    lib_dir = os.path.join(install_dir, 'lib')
    lib_name = None

    if sys.platform == 'darwin':
        lib_name = 'libwebrtc-audio-processing-2.dylib'
    elif sys.platform == 'win32':
        lib_dir = os.path.join(install_dir, 'bin') # DLLs are in bin on Windows
        lib_name = 'webrtc-audio-processing-2.dll'
    else:
        lib_name = 'libwebrtc-audio-processing-2.so'

    # Search for the library file recursively in the lib/bin directory
    lib_paths = glob.glob(os.path.join(lib_dir, '**', lib_name), recursive=True)
    if lib_paths:
        return lib_paths[0]

    # Fallback if not found inside an arch-specific folder
    return os.path.join(lib_dir, lib_name)


def build_webrtc():
    """Builds the WebRTC audio processing library."""
    webrtc_dir = os.path.join(os.path.dirname(__file__), 'webrtc-audio-processing')
    build_dir = os.path.join(webrtc_dir, 'build')
    install_dir = os.path.join(webrtc_dir, 'install')

    if not os.path.exists(os.path.join(webrtc_dir, 'meson.build')):
        print(f"Error: {webrtc_dir}/meson.build not found. "
              "The webrtc-audio-processing submodule may not be properly initialized.", file=sys.stderr)
        # In sdist, we expect the library to be present, so we don't fail hard.
        # The calling function will fail if the library is not found.
        return None

    print("Building WebRTC audio processing library...")

    print("Cleaning up existing build directory...")
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir, exist_ok=True)

    print("Downloading subprojects...")
    subprocess.check_call(['meson', 'subprojects', 'download'], cwd=webrtc_dir)

    print("Configuring build with meson...")
    meson_cmd = [
        'meson', 'setup',
        '--buildtype=release',
        '--default-library=shared',
        '--force-fallback-for=abseil-cpp',
        f'--prefix={install_dir}',
        build_dir
    ]
    subprocess.check_call(meson_cmd, cwd=webrtc_dir)

    print("Building with ninja...")
    subprocess.check_call(['ninja', '-C', build_dir], cwd=webrtc_dir)

    print("Installing...")
    subprocess.check_call(['ninja', '-C', build_dir, 'install'], cwd=webrtc_dir)
    
    if sys.platform == 'win32':
        lib_name = 'webrtc-audio-processing-2-1.dll'
        final_path = os.path.join(install_dir, 'bin', lib_name)
    else:
        final_path = get_webrtc_library_path()
    if not os.path.exists(final_path):
        raise FileNotFoundError(f"Could not find built WebRTC library. Looked for {final_path}")

    print(f"WebRTC library built at {final_path}")
    return final_path


class build_ext(_build_ext):
    def run(self):
        # Build WebRTC library if not found
        lib_path = get_webrtc_library_path()
        if not os.path.exists(lib_path):
            print("Library not found, building WebRTC audio processing library...")
            lib_path = build_webrtc()
        else:
            print(f"Using existing WebRTC library at {lib_path}")

        # Ensure we found the library
        if not lib_path or not os.path.exists(lib_path):
            raise FileNotFoundError("Could not find or build WebRTC library.")

        if sys.platform == 'win32':
            # On Windows, link against the .lib import library.
            webrtc_dir = os.path.join(os.path.dirname(__file__), 'webrtc-audio-processing')
            lib_dir = os.path.join(webrtc_dir, 'install', 'lib')
            self.extensions[0].library_dirs.append(lib_dir)
            # The .lib file does not have the "-1" suffix, so this is correct
            self.extensions[0].libraries.append('webrtc-audio-processing-2')
        else:
            # Original logic for macOS/Linux is preserved
            self.extensions[0].extra_objects = [lib_path]

        # This is the crucial part: copy the library into the build directory
        # so that it gets included in the wheel.
        lib_name = os.path.basename(lib_path)
        dest_pkg_dir = os.path.join(self.build_lib, 'aec_audio_processing', 'files')
        self.mkpath(dest_pkg_dir)
        shutil.copy(lib_path, os.path.join(dest_pkg_dir, lib_name))
        print(f"Copied library to build directory for packaging: {os.path.join(dest_pkg_dir, lib_name)}")

        # Add platform-specific linker args
        if sys.platform == 'win32':
            # No rpath equivalent on Windows; DLLs are found in PATH or next to the .pyd
            pass
        elif sys.platform == 'darwin':
            self.extensions[0].extra_link_args.extend([
                '-Wl,-rpath,@loader_path/files',
                f'-Wl,-install_name,@rpath/files/{lib_name}'
            ])
        else:
            self.extensions[0].extra_link_args.extend([
                '-Wl,-rpath,$ORIGIN/files',
                f'-Wl,-soname,{lib_name}'
            ])

        super().run()


webrtc_dir = os.path.join(os.path.dirname(__file__), 'webrtc-audio-processing')
abseil_dir = os.path.join(webrtc_dir, 'subprojects', 'abseil-cpp-20240722.0')
install_include_dir = os.path.join(webrtc_dir, 'install', 'include')

if not os.path.exists(abseil_dir):
    print(f"Warning: abseil-cpp not found at {abseil_dir}, headers may be missing.", file=sys.stderr)
if not os.path.exists(install_include_dir):
    print(f"Warning: install include directory not found at {install_include_dir}, headers may be missing.", file=sys.stderr)

include_dirs = [
    'src',
    webrtc_dir,
    os.path.join(webrtc_dir, 'webrtc'),
    abseil_dir,
    install_include_dir,
]

swig_opts = [
    '-c++',
    '-Isrc',
    f'-I{webrtc_dir}',
    f'-I{os.path.join(webrtc_dir, "webrtc")}',
    f'-I{abseil_dir}',
    f'-I{install_include_dir}',
]

# EDIT 2: Use the correct C++ standard flag for the MSVC compiler
if sys.platform == 'win32':
    cxx_flags = ['/std:c++20']
else:
    cxx_flags = ['-std=c++17'] # Keep original for other platforms

if sys.platform == 'darwin':
    cxx_flags.extend([
        '-DWEBRTC_AUDIO_PROCESSING_ONLY',
        '-DWEBRTC_NS_FLOAT',
        '-DWEBRTC_POSIX',
        '-DWEBRTC_MAC',
        '-DWEBRTC_HAS_NEON',
        '-DWEBRTC_ARCH_ARM64',
    ])
elif sys.platform == 'win32':
    cxx_flags.extend([
        '-DWEBRTC_AUDIO_PROCESSING_ONLY',
        '-DWEBRTC_NS_FLOAT',
        '-DWEBRTC_WIN',
    ])
else:
    cxx_flags.extend([
        '-DWEBRTC_AUDIO_PROCESSING_ONLY',
        '-DWEBRTC_NS_FLOAT',
        '-DWEBRTC_POSIX',
        '-DWEBRTC_CLOCK_TYPE_REALTIME',
        '-DWEBRTC_LINUX',
        '-DWEBRTC_HAS_NEON',
        '-DWEBRTC_ARCH_ARM64',
    ])

ext_modules = [
    Extension(
        name='aec_audio_processing._webrtc_audio_processing',
        sources=['src/audio_processing.i', 'src/audio_processing_module.cpp'],
        include_dirs=include_dirs,
        swig_opts=swig_opts,
        extra_compile_args=cxx_flags,
        extra_link_args=[],  # Populated by build_ext
        extra_objects=[],    # Populated by build_ext
        language='c++',
    )
]

setup(
    ext_modules=ext_modules,
    cmdclass={
        'build_ext': build_ext,
    },
)