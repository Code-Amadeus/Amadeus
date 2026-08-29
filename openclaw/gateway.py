"""
OpenClaw Gateway 生命周期管理
- start_openclaw_gateway：检测并自动拉起 Gateway 子进程
- stop_openclaw_gateway：退出时安全关闭子进程
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

from config.settings import OPENCLAW_BASE_URL, OPENCLAW_PROJECT_DIR, OPENCLAW_TOKEN

logger = logging.getLogger(__name__)

_openclaw_gateway_proc: asyncio.subprocess.Process | None = None


async def start_openclaw_gateway() -> bool:
    """
    启动 OpenClaw Gateway（若未运行则自动拉起）。
    - 先检测 healthz 端点，已在运行则直接返回 True
    - 未运行则用 subprocess 启动，等待就绪（最多 30 秒）
    - 失败不阻断主程序，仅打印警告
    """
    global _openclaw_gateway_proc

    healthz_url = f"{OPENCLAW_BASE_URL}/healthz"

    # 1. 检测是否已在运行
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(healthz_url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                if r.status == 200:
                    logger.info("[OpenClaw] Gateway is already running; skipping auto-start")
                    return True
    except Exception:
        pass

    # 2. 检查项目目录
    run_script = os.path.join(OPENCLAW_PROJECT_DIR, "scripts", "run-node.mjs")
    if not os.path.exists(run_script):
        logger.warning(f"[OpenClaw] startup script not found: {run_script}; check OPENCLAW_PROJECT_DIR")
        return False

    # 3. 组装环境变量
    env = os.environ.copy()
    env["OPENCLAW_GATEWAY_TOKEN"] = OPENCLAW_TOKEN

    # 4. 启动 Gateway 子进程
    logger.info("[OpenClaw] starting Gateway subprocess...")
    port = OPENCLAW_BASE_URL.rsplit(":", 1)[-1]
    try:
        _openclaw_gateway_proc = await asyncio.create_subprocess_exec(
            "node", run_script,
            "gateway", "run",
            "--port", port,
            "--bind", "loopback",
            cwd=OPENCLAW_PROJECT_DIR,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        logger.warning("[OpenClaw] node executable not found; make sure Node.js is installed and available in PATH")
        return False

    # 5. 等待 Gateway 就绪（轮询 healthz，最多 30 秒）
    for attempt in range(30):
        await asyncio.sleep(1)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(healthz_url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status == 200:
                        logger.info(f"[OpenClaw] Gateway ready ({attempt + 1}s)")
                        return True
        except Exception:
            pass
        if _openclaw_gateway_proc.returncode is not None:
            logger.warning(
                f"[OpenClaw] Gateway process exited unexpectedly, returncode={_openclaw_gateway_proc.returncode}"
            )
            _openclaw_gateway_proc = None
            return False

    logger.warning("[OpenClaw] Gateway startup timed out after 30s; delegation may be unavailable")
    return False


def stop_openclaw_gateway() -> None:
    """退出时关闭由本程序启动的 Gateway 子进程（外部已运行的不干预）。"""
    global _openclaw_gateway_proc
    if _openclaw_gateway_proc is None:
        return
    try:
        _openclaw_gateway_proc.terminate()
        logger.info("[OpenClaw] Gateway subprocess terminated")
    except Exception as e:
        logger.warning(f"[OpenClaw] failed to terminate Gateway subprocess: {e}")
    _openclaw_gateway_proc = None
