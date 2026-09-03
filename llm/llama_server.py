"""
本地 llama-server 生命周期管理
- start_llama_server：检测端口 → 拉起子进程 → 健康检查等待
- warmup_local_llm_cache：静默预热 Prompt Cache
- stop_llama_server：安全终止子进程
"""
from __future__ import annotations

import asyncio
import argparse
import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from config.settings import (
    HYBRID_LOCAL_LLM_MODEL,
    HYBRID_LOCAL_LLM_URL,
    LOCAL_LLM_URL,
    LOCAL_LLM_CLI_PATH,
    LOCAL_LLM_SERVER_ARGS,
    LOCAL_LLM_MODEL_PATH,
    LOCAL_LLM_MODEL,
    LOCAL_LLM_CUDA_VISIBLE_DEVICES,
)
from llm.local_backends import openai_chat_url, should_manage_local_server

logger = logging.getLogger(__name__)

_llm_server_proc: subprocess.Popen | None = None
_llm_server_log = None
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolved_file(value: str) -> Path:
    path = Path(str(value or "").strip())
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _profile_values(profile: str) -> tuple[str, str]:
    clean = str(profile or "local").strip().lower()
    if clean == "hybrid":
        return HYBRID_LOCAL_LLM_URL, HYBRID_LOCAL_LLM_MODEL
    if clean == "local":
        return LOCAL_LLM_URL, LOCAL_LLM_MODEL
    raise ValueError(f"unsupported llama-server profile: {profile!r}")


def _endpoint_port(endpoint: str) -> int:
    parsed = urlparse(str(endpoint or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"llama-server endpoint must be an HTTP(S) URL: {endpoint!r}")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("managed llama-server endpoint must use a loopback host")
    return int(parsed.port or (443 if parsed.scheme == "https" else 80))


def _replace_option(args: list[str], option: str, value: str) -> None:
    try:
        index = args.index(option)
    except ValueError:
        args.extend([option, value])
        return
    if index + 1 >= len(args):
        args.append(value)
    else:
        args[index + 1] = value


def build_llama_server_command(profile: str = "local") -> list[str]:
    endpoint, model_alias = _profile_values(profile)
    executable = _resolved_file(LOCAL_LLM_CLI_PATH)
    model_file = _resolved_file(LOCAL_LLM_MODEL_PATH)
    if not str(LOCAL_LLM_CLI_PATH or "").strip() or not executable.is_file():
        raise FileNotFoundError(f"llama-server executable not found: {executable}")
    if not str(LOCAL_LLM_MODEL_PATH or "").strip() or not model_file.is_file():
        raise FileNotFoundError(f"GGUF model not found: {model_file}")
    args = list(LOCAL_LLM_SERVER_ARGS)
    _replace_option(args, "-m", str(model_file))
    _replace_option(args, "--port", str(_endpoint_port(endpoint)))
    _replace_option(args, "-a", str(model_alias))
    return [str(executable), *args]


def _server_environment(server_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    runtime_dirs = _candidate_torch_lib_dirs(str(server_dir))
    env["PATH"] = os.pathsep.join([str(server_dir), *runtime_dirs, env.get("PATH", "")])
    if LOCAL_LLM_CUDA_VISIBLE_DEVICES:
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["CUDA_VISIBLE_DEVICES"] = LOCAL_LLM_CUDA_VISIBLE_DEVICES
        logger.info(
            "llama-server CUDA_VISIBLE_DEVICES=%s",
            LOCAL_LLM_CUDA_VISIBLE_DEVICES,
        )
    return env


def _health_url(endpoint: str) -> str:
    return str(endpoint or "").strip().rstrip("/").removesuffix("/v1") + "/health"


def _candidate_torch_lib_dirs(server_dir: str) -> list[str]:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(repo_root, ".venv", "Lib", "site-packages", "torch", "lib"),
        os.path.join(os.environ.get("VIRTUAL_ENV", ""), "Lib", "site-packages", "torch", "lib"),
        os.path.join(server_dir, "torch", "lib"),
    ]
    result: list[str] = []
    for path in candidates:
        if path and os.path.exists(os.path.join(path, "cudart64_12.dll")) and path not in result:
            result.append(path)
    return result


async def start_llama_server() -> None:
    """Start the managed pure-local llama-server when it is not already online."""
    global _llm_server_proc, _llm_server_log

    health_url = _health_url(LOCAL_LLM_URL)

    # 1. 优先检查端口：已有服务则直接复用
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=1)) as resp:
                if resp.status == 200:
                    logger.info("detected an existing llama-server process; connecting directly")
                    return
    except Exception:
        pass  # 端口未通，继续启动

    if _llm_server_proc is not None and _llm_server_proc.poll() is None:
        logger.info("runtime log event at llm/llama_server.py:48")
        return

    try:
        cmd = build_llama_server_command("local")
        server_dir = Path(cmd[0]).parent
        env = _server_environment(server_dir)
        logger.info("starting managed llama-server: %s", cmd[0])
        _llm_server_log = open(_PROJECT_ROOT / "llama_server.log", "w", encoding="utf-8")
        _llm_server_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=_llm_server_log,
            stderr=subprocess.STDOUT,
            cwd=str(server_dir),
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        # Poll health for at most 15 seconds.
        for _ in range(30):
            if _llm_server_proc.poll() is not None:
                logger.error(
                    f"❌ llama-server 启动后立即退出，返回码: {_llm_server_proc.returncode}"
                )
                if _llm_server_log is not None:
                    _llm_server_log.close()
                    _llm_server_log = None
                _llm_server_proc = None
                return
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        health_url, timeout=aiohttp.ClientTimeout(total=1)
                    ) as resp:
                        if resp.status == 200:
                            logger.info("managed llama-server is ready: %s", health_url)
                            return
            except Exception:
                pass
            await asyncio.sleep(0.5)

        logger.warning("managed llama-server did not become ready: %s", health_url)
        stop_llama_server()

    except Exception:
        logger.exception("managed llama-server failed to start")
        _llm_server_proc = None
        if _llm_server_log is not None:
            _llm_server_log.close()
            _llm_server_log = None


async def warmup_local_llm_cache() -> None:
    """
    本地 llama-server Prompt Cache 静默预热。
    仅在由 Amadeus 托管纯本地 llama-server 时触发；不写入会话历史。
    """
    from config import settings

    if not should_manage_local_server(settings):
        return

    try:
        api_url = openai_chat_url(LOCAL_LLM_URL)

        system_prompt = (
            "あなたは牧瀬紅莉栖で,優秀で理知的な性格です.少しツンデレで,でも根は優しい."
            "必ず日本語のみで短く自然に答えてください。"
        )
        warmup_user = "これは事前ウォームアップ用のテストです。一言だけ返事してください。"

        payload = {
            "model": LOCAL_LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": warmup_user},
            ],
            "stream": False,
            "temperature": 0.1,
            "max_tokens": 32,
            "cache_prompt": True,
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(api_url, json=payload) as resp:
                if resp.status == 200:
                    await resp.json()
                    logger.info("runtime log event at llm/llama_server.py:150")
                else:
                    logger.warning("runtime log event at llm/llama_server.py:152")
    except Exception:
        logger.warning("runtime log event at llm/llama_server.py:154")


def stop_llama_server() -> None:
    """Stop only the llama-server process started by this module."""
    global _llm_server_proc, _llm_server_log
    if not _llm_server_proc:
        return
    logger.info("runtime log event at llm/llama_server.py:162")
    try:
        _llm_server_proc.terminate()
        try:
            _llm_server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _llm_server_proc.kill()
        logger.info("runtime log event at llm/llama_server.py:169")
    except Exception:
        logger.error("runtime log event at llm/llama_server.py:171")
    finally:
        _llm_server_proc = None
        if _llm_server_log is not None:
            _llm_server_log.close()
            _llm_server_log = None


def run_llama_server_foreground(profile: str = "local") -> int:
    """Run the configured server visibly for the repository BAT launchers."""
    cmd = build_llama_server_command(profile)
    server_dir = Path(cmd[0]).parent
    endpoint, model_alias = _profile_values(profile)
    print(f"Starting llama.cpp server profile={profile}")
    print(f"Endpoint: {endpoint}")
    print(f"Model alias: {model_alias}")
    print(f"Executable: {cmd[0]}")
    try:
        return int(
            subprocess.call(
                cmd,
                cwd=str(server_dir),
                env=_server_environment(server_dir),
            )
        )
    except KeyboardInterrupt:
        return 130


def _main() -> int:
    parser = argparse.ArgumentParser(description="Start the configured llama.cpp server")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--profile", choices=("local", "hybrid"), default="local")
    args = parser.parse_args()
    if not args.foreground:
        parser.error("--foreground is required when running this module directly")
    try:
        return run_llama_server_foreground(args.profile)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
