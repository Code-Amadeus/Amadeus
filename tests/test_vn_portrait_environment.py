"""Check diagnostic failures and cleanup; native GUI proof runs separately in CI."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools import verify_vn_portrait_environment as checker


@pytest.fixture
def native_boundary(monkeypatch):
    root = MagicMock()
    root.tk.call.side_effect = lambda *args: {
        ("info", "patchlevel"): "8.6.14",
        ("package", "provide", "Tk"): "8.6.14",
        ("photo", "get", 0, 0): (20, 40, 60),
        ("photo", "transparency", "get", 1, 1): 1,
    }[args]
    root.tk.getboolean.side_effect = bool
    photo = MagicMock()
    photo.__str__.return_value = "photo"
    photo.width.return_value = photo.height.return_value = 2
    tk = SimpleNamespace(Tk=MagicMock(return_value=root))
    image_tk = SimpleNamespace(PhotoImage=MagicMock(return_value=photo))
    monkeypatch.setitem(sys.modules, "_tkinter", SimpleNamespace(
        __spec__=SimpleNamespace(origin="test-extension")))
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "PIL.ImageTk", image_tk)
    import PIL
    monkeypatch.setattr(PIL, "ImageTk", image_tk, raising=False)
    return tk, image_tk, root


def test_success_requires_image_transfer_and_releases_window(native_boundary, capsys):
    _, image_tk, root = native_boundary
    assert checker.main() == 0
    image_tk.PhotoImage.assert_called_once()
    root.destroy.assert_called_once()
    assert "environment ok" in capsys.readouterr().out


@pytest.mark.parametrize("stage", ["imports", "Tk initialization", "ImageTk transfer"])
def test_failure_is_nonzero_and_identifies_boundary(native_boundary, monkeypatch, capsys, stage):
    tk, image_tk, root = native_boundary
    if stage == "imports":
        monkeypatch.setitem(sys.modules, "tkinter", None)
    elif stage == "Tk initialization":
        tk.Tk.side_effect = RuntimeError("Can't find a usable init.tcl")
    else:
        image_tk.PhotoImage.side_effect = TypeError("bad argument type for built-in operation")
    assert checker.main() == 1
    output = capsys.readouterr()
    assert stage in output.err
    assert "MACOS_INSTALL.md" in output.err
    assert "environment ok" not in output.out
    assert root.destroy.call_count == (1 if stage == "ImageTk transfer" else 0)


@pytest.mark.parametrize("incorrect", ["pixels", "alpha"])
def test_image_creation_alone_does_not_prove_transfer(native_boundary, capsys, incorrect):
    _, _, root = native_boundary
    native_call = root.tk.call.side_effect
    def call(*args):
        if args == ("photo", "get", 0, 0) and incorrect == "pixels":
            return (0, 0, 0)
        if args == ("photo", "transparency", "get", 1, 1) and incorrect == "alpha":
            return 0
        return native_call(*args)
    root.tk.call.side_effect = call
    assert checker.main() == 1
    assert "ImageTk transfer" in capsys.readouterr().err
    root.destroy.assert_called_once()
