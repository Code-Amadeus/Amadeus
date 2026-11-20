# Copyright (c) 2024 NVIDIA CORPORATION.
#   Licensed under the MIT license.

import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys

import torch
from torch.utils import cpp_extension

"""
Setting this param to a list has a problem of generating different compilation commands (with diferent order of architectures) and leading to recompilation of fused kernels. 
Set it to empty stringo avoid recompilation and assign arch flags explicity in extra_cuda_cflags below
"""
os.environ["TORCH_CUDA_ARCH_LIST"] = ""


def _get_tts_device_index() -> int:
    """从 TTS_DEVICE 环境变量解析设备序号，默认 0。"""
    tts_device = os.environ.get("TTS_DEVICE", "cuda:0")
    try:
        return int(tts_device.split(":")[-1])
    except (ValueError, IndexError):
        return 0


def _sanitize_cache_token(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip().lower())
    return value.strip("_") or "unknown"


def _get_gpu_cache_suffix(device_idx: int) -> str:
    override = os.environ.get("BIGVGAN_CACHE_ID", "").strip()
    if override:
        return _sanitize_cache_token(override)

    if not torch.cuda.is_available():
        return f"device{device_idx}"

    try:
        props = torch.cuda.get_device_properties(device_idx)
    except Exception:
        return f"device{device_idx}"

    name = _sanitize_cache_token(getattr(props, "name", f"device{device_idx}"))
    major = getattr(props, "major", "x")
    minor = getattr(props, "minor", "x")
    total_gb = int(round(getattr(props, "total_memory", 0) / (1024 ** 3)))
    py_tag = f"py{sys.version_info.major}{sys.version_info.minor}"
    torch_tag = _sanitize_cache_token(f"torch{torch.__version__.split('+', 1)[0]}")
    cuda_tag = _sanitize_cache_token(f"cu{torch.version.cuda or 'cpu'}")
    return f"sm{major}{minor}_{total_gb}gb_{name}_{py_tag}_{torch_tag}_{cuda_tag}"


def _find_vcvars64():
    override = os.environ.get("MSVC_VCVARS64_PATH", "").strip()
    if override:
        path = pathlib.Path(override)
        if path.exists():
            return path

    vswhere = pathlib.Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    vswhere = vswhere / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.exists():
        try:
            install_path = subprocess.check_output(
                [
                    str(vswhere),
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property",
                    "installationPath",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if install_path:
                candidate = pathlib.Path(install_path) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
                if candidate.exists():
                    return candidate
        except Exception:
            pass

    candidates = [
        pathlib.Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"),
        pathlib.Path(r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"),
        pathlib.Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"),
        pathlib.Path(r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _ensure_msvc_on_path() -> None:
    if os.name != "nt" or shutil.which("cl"):
        return

    vcvars64 = _find_vcvars64()
    if vcvars64 is None:
        print("[BigVGAN] MSVC cl.exe not found and vcvars64.bat was not discovered")
        return

    def _short_windows_path(path: pathlib.Path) -> str:
        try:
            import ctypes

            text = str(path)
            buffer = ctypes.create_unicode_buffer(260)
            result = ctypes.windll.kernel32.GetShortPathNameW(text, buffer, len(buffer))
            if result:
                return buffer.value
        except Exception:
            pass
        return str(path)

    vcvars_cmd = _short_windows_path(vcvars64)

    try:
        output = subprocess.check_output(
            ["cmd.exe", "/d", "/c", f"call {vcvars_cmd} >nul && set"],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.output or "").strip().splitlines()
        detail = detail[-1] if detail else str(exc)
        print(f"[BigVGAN] Failed to import MSVC environment from {vcvars64}: {detail}")
        return
    except Exception as exc:
        print(f"[BigVGAN] Failed to import MSVC environment from {vcvars64}: {exc}")
        return

    updates = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.upper() == "PATH":
            # cmd/set can emit both PATH and Path. On Windows Python treats
            # env keys case-insensitively, so keep the first vcvars-expanded
            # PATH and ignore the later pre-vcvars alias.
            updates.setdefault("PATH", value)
        else:
            updates[key] = value

    os.environ.update(updates)

    if shutil.which("cl"):
        print(f"[BigVGAN] MSVC build environment loaded: {vcvars64}")
    else:
        print(f"[BigVGAN] vcvars64.bat loaded but cl.exe is still unavailable: {vcvars64}")


def load():
    # Build path — per-device cache so switching GPU doesn't break the kernel
    device_idx = _get_tts_device_index()
    srcpath = pathlib.Path(__file__).parent.absolute()
    buildpath = srcpath / f"build_{_get_gpu_cache_suffix(device_idx)}"
    _create_build_dir(buildpath)
    print(f"[BigVGAN] CUDA kernel cache dir: {buildpath} (TTS_DEVICE=cuda:{device_idx})")

    # Helper function to build the kernels.
    def _cpp_extention_load_helper(name, sources, extra_cuda_flags):
        # On Windows + venv, python3xx.lib lives in the base Python install
        # (sys.base_prefix/libs), NOT in .venv/Scripts/libs.
        # torch's cpp_extension only adds the venv path, so we add the base path
        # manually via extra_ldflags to avoid LNK1104.
        extra_ldflags = []
        if os.name == "nt":
            import sys
            base_libs = os.path.join(sys.base_prefix, "libs")
            if os.path.isdir(base_libs):
                extra_ldflags.append(f"/LIBPATH:{base_libs}")

        return cpp_extension.load(
            name=name,
            sources=sources,
            build_directory=buildpath,
            extra_cflags=[
                "-O3",
            ],
            extra_cuda_cflags=[
                "-O3",
                "-gencode",
                "arch=compute_70,code=sm_70",
                "--use_fast_math",
            ]
            + extra_cuda_flags
            + cc_flag,
            extra_ldflags=extra_ldflags,
            verbose=True,
        )

    extra_cuda_flags = [
        "-U__CUDA_NO_HALF_OPERATORS__",
        "-U__CUDA_NO_HALF_CONVERSIONS__",
        "--expt-relaxed-constexpr",
        "--expt-extended-lambda",
        "-allow-unsupported-compiler",
    ]

    # 优先加载已编译的缓存 .pyd，跳过编译流程（无需 cl.exe/ninja）
    # 缓存目录按设备隔离（build_device0 / build_device1 ...），切换 GPU 自动重编译
    pyd_path = buildpath / "anti_alias_activation_cuda.pyd"
    if pyd_path.exists():
        spec = importlib.util.spec_from_file_location("anti_alias_activation_cuda", pyd_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print(f"[BigVGAN] loading CUDA kernel from cache: {pyd_path}")
        return module

    # Check the local CUDA toolkit only when a new build is required.
    cc_flag = []
    _, bare_metal_major, _ = _get_cuda_bare_metal_version(cpp_extension.CUDA_HOME)
    if int(bare_metal_major) >= 11:
        cc_flag.append("-gencode")
        cc_flag.append("arch=compute_80,code=sm_80")

    _ensure_msvc_on_path()

    sources = [
        srcpath / "anti_alias_activation.cpp",
        srcpath / "anti_alias_activation_cuda.cu",
    ]
    anti_alias_activation_cuda = _cpp_extention_load_helper(
        "anti_alias_activation_cuda", sources, extra_cuda_flags
    )

    return anti_alias_activation_cuda


def _get_cuda_bare_metal_version(cuda_dir):
    raw_output = subprocess.check_output(
        [cuda_dir + "/bin/nvcc", "-V"], universal_newlines=True
    )
    output = raw_output.split()
    release_idx = output.index("release") + 1
    release = output[release_idx].split(".")
    bare_metal_major = release[0]
    bare_metal_minor = release[1][0]

    return raw_output, bare_metal_major, bare_metal_minor


def _create_build_dir(buildpath):
    try:
        os.mkdir(buildpath)
    except OSError:
        if not os.path.isdir(buildpath):
            print(f"Creation of the build directory {buildpath} failed")
