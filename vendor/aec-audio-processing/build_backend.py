"""Run the vendored setuptools build in a disposable source copy.

Meson and setuptools both write beside their inputs. A PEP 517 path dependency
must not let those writes mutate Amadeus's checked-in third-party sources.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile

from setuptools import build_meta


SOURCE = Path(__file__).resolve().parent


def _ignore_build_outputs(directory, names):
    ignored = {"__pycache__", ".git", ".pytest_cache"}
    directory = Path(directory)
    if directory == SOURCE:
        ignored.update({"build", "dist", ".eggs"})
        ignored.update(name for name in names if name.endswith(".egg-info"))
    if directory == SOURCE / "webrtc-audio-processing":
        ignored.update({"build", "install"})
    return ignored.intersection(names)


@contextmanager
def _staged_source():
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="amadeus-aec-build-") as directory:
        staging = Path(directory) / "source"
        shutil.copytree(SOURCE, staging, ignore=_ignore_build_outputs)
        try:
            os.chdir(staging)
            yield
        finally:
            os.chdir(previous)


def get_requires_for_build_wheel(config_settings=None):
    with _staged_source():
        return build_meta.get_requires_for_build_wheel(config_settings)


def get_requires_for_build_sdist(config_settings=None):
    with _staged_source():
        return build_meta.get_requires_for_build_sdist(config_settings)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    metadata_directory = str(Path(metadata_directory).resolve())
    with _staged_source():
        return build_meta.prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    wheel_directory = str(Path(wheel_directory).resolve())
    if metadata_directory is not None:
        metadata_directory = str(Path(metadata_directory).resolve())
    with _staged_source():
        return build_meta.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    sdist_directory = str(Path(sdist_directory).resolve())
    with _staged_source():
        return build_meta.build_sdist(sdist_directory, config_settings)
