import ctypes
import os
import platform

script_dir = os.path.dirname(os.path.abspath(__file__))

sys_name = platform.system()
if sys_name == "Darwin":
    lib_name = "libwebrtc-audio-processing-2.dylib"
elif sys_name == "Linux":
    lib_name = "libwebrtc-audio-processing-2.so"
elif sys_name == "Windows":
    lib_name = "webrtc-audio-processing-2-1.dll"
else:
    raise OSError(f"Unsupported OS: {sys_name}")

lib_path = os.path.join(script_dir, "files", lib_name)

try:
    lib = ctypes.cdll.LoadLibrary(lib_path)
except OSError as e:
    raise OSError(
        f"Error loading webrtc library at {lib_path}. "
        f"It may be corrupted or incompatible with your platform. "
        f"Original error: {e}"
    ) from e