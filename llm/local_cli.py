"""
CLI 模式本地 LLM 调用（llama-cli 进程管理）
- local_llm_query_cli_stream：流式异步生成器，逐字符 yield
- local_llm_query_cli：非流式版本，返回完整字符串

进程复用策略：_llm_cli_proc 为模块级单例，跨调用复用；
出错时自动清理并置 None，下次调用重新启动。
"""
from __future__ import annotations

import asyncio
import logging
import time

from config.log_privacy import protected_text
from config.settings import (
    LOCAL_LLM_CLI_PATH,
    LOCAL_LLM_CLI_ARGS,
    LOCAL_LLM_CLI_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

_llm_cli_proc: asyncio.subprocess.Process | None = None


async def local_llm_query_cli_stream(question: str, system_prompt: str = None):
    """
    通过 CLI 调用本地 LLM（流式异步生成器）。

    llama-cli 使用方式：
    - 通过命令行参数启动，指定模型等参数
    - 通过 stdin 发送文本提示（换行结束）
    - 通过 stdout 接收流式输出
    - 以 Qwen3 专用 EOS token 或反向提示 "User:" 判断输出结束
    """
    global _llm_cli_proc

    if not LOCAL_LLM_CLI_PATH:
        raise ValueError("LOCAL_LLM_CLI_PATH 未配置，请设置CLI可执行文件路径")

    if system_prompt is None:
        system_prompt = LOCAL_LLM_CLI_SYSTEM_PROMPT

    full_prompt = f"{system_prompt}\n\nUser: {question}\nAssistant:"
    logger.info(
        "sending full prompt (including system): %s",
        protected_text(question, limit=50),
    )

    try:
        if _llm_cli_proc is None or _llm_cli_proc.returncode is not None:
            logger.info("runtime log event at llm/local_cli.py:49")
            cmd = [LOCAL_LLM_CLI_PATH] + (LOCAL_LLM_CLI_ARGS or [])
            _llm_cli_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("runtime log event at llm/local_cli.py:57")

        # 读取初始化 stderr
        stderr_buffer: list[str] = []

        async def read_stderr_initial():
            if _llm_cli_proc.stderr:
                try:
                    for _ in range(100):
                        try:
                            line = await asyncio.wait_for(
                                _llm_cli_proc.stderr.readline(), timeout=0.1
                            )
                            if not line:
                                break
                            text = line.decode("utf-8", errors="replace").strip()
                            if text:
                                stderr_buffer.append(text)
                                logger.info("CLI stderr: %s", protected_text(text, limit=160))
                        except asyncio.TimeoutError:
                            break
                except Exception:
                    logger.debug("runtime log event at llm/local_cli.py:79")

        logger.info("runtime log event at llm/local_cli.py:81")
        await asyncio.wait_for(read_stderr_initial(), timeout=5.0)
        logger.info("runtime log event at llm/local_cli.py:83")

        if _llm_cli_proc.stdin:
            prompt_bytes = full_prompt.encode("utf-8") + b"\n"
            _llm_cli_proc.stdin.write(prompt_bytes)
            await _llm_cli_proc.stdin.drain()
            logger.info("runtime log event at llm/local_cli.py:89")

        await asyncio.sleep(0.5)

        # 连续读取 stderr（调试用）
        async def read_stderr_continuous():
            if _llm_cli_proc.stderr:
                try:
                    while True:
                        try:
                            line = await asyncio.wait_for(
                                _llm_cli_proc.stderr.readline(), timeout=0.5
                            )
                            if not line:
                                break
                            text = line.decode("utf-8", errors="replace").strip()
                            if text:
                                logger.info("CLI stderr: %s", protected_text(text, limit=160))
                        except asyncio.TimeoutError:
                            if _llm_cli_proc.returncode is not None:
                                break
                except Exception:
                    logger.debug("runtime log event at llm/local_cli.py:111")

        stderr_task = asyncio.create_task(read_stderr_continuous())

        # 流式读取 stdout
        buffer = ""
        qwen_eos_tokens = ["<|im_end|>", "<|endoftext|>"]
        reverse_prompt = "User:"
        found_assistant = False
        timeout_count = 0
        max_timeout = 300
        bytes_read = 0
        last_content_time = time.time()
        max_output_length = 5000

        if _llm_cli_proc.stdout:
            logger.info("runtime log event at llm/local_cli.py:127")
            while True:
                try:
                    line = await asyncio.wait_for(
                        _llm_cli_proc.stdout.readline(), timeout=0.1
                    )
                except asyncio.TimeoutError:
                    timeout_count += 1
                    if timeout_count % 50 == 0:
                        logger.info(
                            f"⏳ 等待CLI输出中... (已等待 {timeout_count * 0.1:.1f}秒, "
                            f"buffer长度: {len(buffer)})"
                        )
                    if timeout_count > max_timeout:
                        logger.warning(
                            f"⚠️ CLI读取超时，可能没有输出。buffer内容: {buffer[:200]}"
                        )
                        break
                    if _llm_cli_proc.returncode is not None:
                        logger.info("runtime log event at llm/local_cli.py:146")
                        break
                    continue

                if not line:
                    if _llm_cli_proc.returncode is not None:
                        await asyncio.sleep(0.2)
                        try:
                            remaining = await asyncio.wait_for(
                                _llm_cli_proc.stdout.read(4096), timeout=0.1
                            )
                            if remaining:
                                line = remaining
                                logger.info("runtime log event at llm/local_cli.py:159")
                            else:
                                break
                        except asyncio.TimeoutError:
                            break
                    else:
                        continue

                bytes_read += len(line)
                try:
                    text = line.decode("utf-8", errors="replace")
                    buffer += text

                    if bytes_read < 500:
                        logger.info("runtime log event at llm/local_cli.py:173")

                    if not found_assistant:
                        if text.strip().startswith("> "):
                            after_prompt = text[2:].strip()
                            if (
                                after_prompt
                                and not after_prompt.startswith("User:")
                                and len(after_prompt) > 0
                            ):
                                found_assistant = True
                                logger.info(
                                    f"✅ 找到模型输出标记 '> '！开始输出: {after_prompt[:50]}..."
                                )
                                for char in after_prompt:
                                    if char and char != "\r":
                                        yield char
                                if text.endswith("\n"):
                                    yield "\n"
                                continue
                        continue

                    if len(buffer) > max_output_length:
                        logger.warning(
                            f"⚠️ 输出长度超过限制 ({max_output_length}字符)，强制停止"
                        )
                        break

                    # Qwen3 EOS 检测
                    found_eos = False
                    eos_token = None
                    for eos in qwen_eos_tokens:
                        if eos in text:
                            found_eos = True
                            eos_token = eos
                            logger.info("runtime log event at llm/local_cli.py:208")
                            break

                    if found_eos:
                        end_idx = text.find(eos_token)
                        if end_idx > 0:
                            content = text[:end_idx].strip()
                            if content.startswith("> "):
                                content = content[2:].strip()
                            if content:
                                for char in content:
                                    if char and char != "\r":
                                        yield char
                        break

                    # 反向提示备用检测
                    text_stripped = text.strip()
                    text_lower = text.lower()
                    if reverse_prompt.lower() in text_lower or "> user:" in text_lower:
                        is_reverse = False
                        if text_stripped in (reverse_prompt, "> " + reverse_prompt):
                            is_reverse = True
                            logger.info("runtime log event at llm/local_cli.py:230")
                        elif (
                            text_stripped.startswith("> " + reverse_prompt)
                            or text_stripped.startswith(reverse_prompt)
                        ) and len(buffer) > 10:
                            is_reverse = True
                            logger.info("runtime log event at llm/local_cli.py:236")

                        if is_reverse:
                            end_idx = -1
                            for pattern in [
                                reverse_prompt,
                                "> " + reverse_prompt,
                                ">User:",
                                "> User:",
                            ]:
                                idx = text.find(pattern)
                                if idx >= 0:
                                    end_idx = idx
                                    break
                            if end_idx > 0:
                                content = text[:end_idx].strip()
                                if content.startswith("> "):
                                    content = content[2:].strip()
                                if content:
                                    for char in content:
                                        if char and char != "\r":
                                            yield char
                            break

                    if text_stripped and text_stripped not in [">", "> "]:
                        last_content_time = time.time()

                    if (
                        time.time() - last_content_time > 10.0
                        and found_assistant
                        and len(buffer) > 100
                    ):
                        logger.info(
                            f"✅ 超过10秒未收到新内容（已有{len(buffer)}字符），可能输出已结束"
                        )
                        break

                    line_content = text
                    if line_content.strip().startswith("> "):
                        line_content = line_content[2:].lstrip()
                        if line_content.strip():
                            for char in line_content:
                                if char and char != "\r":
                                    yield char
                            if text.endswith("\n"):
                                yield "\n"
                    elif line_content.strip() in [">", "> "]:
                        continue
                    else:
                        for char in line_content:
                            if char and char != "\r":
                                yield char

                except UnicodeDecodeError:
                    logger.warning("runtime log event at llm/local_cli.py:290")
                    continue
                except Exception:
                    logger.error("runtime log event at llm/local_cli.py:293")
                    continue

            logger.info("runtime log event at llm/local_cli.py:296")

        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

    except Exception:
        logger.error("runtime log event at llm/local_cli.py:305")
        if _llm_cli_proc:
            try:
                _llm_cli_proc.terminate()
                await _llm_cli_proc.wait()
            except Exception:
                pass
            _llm_cli_proc = None
        raise


async def local_llm_query_cli(
    question: str, stream: bool = False, system_prompt: str = None
) -> str:
    """
    通过 CLI 调用本地 LLM（非流式版本）。
    收集所有输出直到遇到反向提示，返回完整字符串。
    """
    global _llm_cli_proc

    if not LOCAL_LLM_CLI_PATH:
        raise ValueError("LOCAL_LLM_CLI_PATH 未配置，请设置CLI可执行文件路径")

    if system_prompt is None:
        system_prompt = LOCAL_LLM_CLI_SYSTEM_PROMPT

    full_prompt = f"{system_prompt}\n\nUser: {question}\nAssistant:"

    try:
        if _llm_cli_proc is None or _llm_cli_proc.returncode is not None:
            logger.info("runtime log event at llm/local_cli.py:335")
            cmd = [LOCAL_LLM_CLI_PATH] + (LOCAL_LLM_CLI_ARGS or [])
            _llm_cli_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("runtime log event at llm/local_cli.py:343")

        if _llm_cli_proc.stdin:
            prompt_bytes = full_prompt.encode("utf-8") + b"\n"
            _llm_cli_proc.stdin.write(prompt_bytes)
            await _llm_cli_proc.stdin.drain()

        full_response = ""
        buffer = ""
        reverse_prompt = "User:"

        if _llm_cli_proc.stdout:
            while True:
                chunk = await _llm_cli_proc.stdout.read(1024)
                if not chunk:
                    break
                try:
                    text = chunk.decode("utf-8", errors="replace")
                    buffer += text
                    if reverse_prompt in buffer:
                        end_idx = buffer.find(reverse_prompt)
                        if end_idx > 0:
                            full_response = buffer[:end_idx].strip()
                        break
                except Exception:
                    logger.error("runtime log event at llm/local_cli.py:368")
                    break

        return full_response if full_response else buffer.strip()

    except Exception:
        logger.error("runtime log event at llm/local_cli.py:374")
        if _llm_cli_proc:
            try:
                _llm_cli_proc.terminate()
                await _llm_cli_proc.wait()
            except Exception:
                pass
            _llm_cli_proc = None
        raise
