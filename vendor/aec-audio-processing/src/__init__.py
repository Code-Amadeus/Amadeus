import os
import sys

if sys.platform == "win32" and sys.version_info >= (3, 8):
    dll_path = os.path.join(os.path.dirname(__file__), "files")
    if os.path.isdir(dll_path):
        os.add_dll_directory(dll_path)

from .loader import lib  # Import the library first
from .audio_processing import AudioProcessor
from ._version import __version__, version_info

__all__ = ['AudioProcessor', '__version__', 'version_info', 'lib']