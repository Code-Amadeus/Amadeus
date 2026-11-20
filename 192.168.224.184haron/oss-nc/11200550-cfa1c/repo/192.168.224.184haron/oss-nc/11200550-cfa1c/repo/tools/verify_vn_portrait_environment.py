"""Exercise the VN portrait's native Tk/ImageTk boundary without character assets.

Run from a graphical desktop session. This is deliberately separate from the
headless package/profile verifier; imports alone cannot qualify this boundary.
"""
from __future__ import annotations

import platform
import sys


def verify() -> None:
    stage = "imports"
    root = None
    try:
        import _tkinter
        import tkinter as tk
        from PIL import Image, ImageTk, __version__ as pillow_version

        print(f"Python: {sys.version}")
        print(f"Platform: {platform.system()} {platform.machine()}")
        print(f"Pillow: {pillow_version}; _tkinter: {_tkinter.__spec__.origin}")
        stage = "Tk initialization"
        root = tk.Tk()
        root.withdraw()
        print(f"Tcl: {root.tk.call('info', 'patchlevel')}; "
              f"Tk: {root.tk.call('package', 'provide', 'Tk')}")
        stage = "ImageTk transfer"
        with Image.new("RGBA", (2, 2), (20, 40, 60, 255)) as frame:
            frame.putpixel((1, 1), (0, 0, 0, 0))
            photo = ImageTk.PhotoImage(frame, master=root)
        if (photo.width(), photo.height()) != (2, 2):
            raise RuntimeError("transferred image dimensions differ")
        if tuple(root.tk.call(str(photo), "get", 0, 0)) != (20, 40, 60):
            raise RuntimeError("transferred image pixels differ")
        if not root.tk.getboolean(root.tk.call(str(photo), "transparency", "get", 1, 1)):
            raise RuntimeError("transferred image lost transparency")
        root.update_idletasks()
        del photo
    except Exception as exc:
        raise RuntimeError(f"VN portrait check failed during {stage}: {exc}") from exc
    finally:
        if root is not None:
            root.destroy()
    print("VN portrait environment ok: Tk initialization and ImageTk RGBA transfer passed")


def main() -> int:
    try:
        verify()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        print("Run in a graphical desktop session. For macOS interpreter migration, "
              "see MACOS_INSTALL.md#vn-portrait-tk.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
