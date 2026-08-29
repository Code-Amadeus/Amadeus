# -*- coding: utf-8 -*-
"""agent_host/event_reactor.py

Amadeus Agent 事件反应器
========================
接收来自 agent 适配层的工具事件，
聚合任务过程信息，为 Amadeus 解释层提供输入。

与适配层的边界：
  - 适配层（openclaw/client.py）负责从各 agent 拿到原始事件
  - 本模块负责 Amadeus 侧的处理：日志、聚合、摘要、触发判断
  - 不依赖任何具体 agent 实现，只消费标准化的 event dict

计划中的处理流程：
    1. on_tool_event()   — 接收原始事件，追加到事件列表
    2. on_complete()     — 任务结束，生成上下文摘要（TODO）
    3. 触发判断          — 决定是否通知角色层（TODO：触发策略）
    4. 注入 system ctx   — 把摘要 patch 进主 LLM 的 system context（TODO）

使用方式：
    from agent_host.event_reactor import AgentEventReactor
    from openclaw.client import ask_openclaw_stream

    reactor = AgentEventReactor(task="...", source="openclaw")
    result = await ask_openclaw_stream(
        task,
        chunk_callback=...,
        tool_event_callback=reactor.on_tool_event,
    )
    reactor.on_complete(result)
"""
from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


class AgentEventReactor:
    """单次 agent 任务的事件反应器。每次任务创建一个新实例。

    参数：
        task    — 原始任务描述，用于摘要生成
        source  — provider 来源标识，方便日志区分
        run_id  — 可选，透传 agent 侧的 run_id
        notify_character — 预留回调，触发策略确定后注入
    """

    def __init__(
        self,
        task: str,
        source: str = "agent",
        run_id: str | None = None,
        notify_character: Callable[[str], None] | None = None,
    ):
        self.task = task
        self.source = source
        self.run_id = run_id or f"{source}_{int(time.time() * 1000)}"
        self._notify_character = notify_character
        self._events: list[dict] = []
        self._started_at = time.monotonic()

    # ── 事件接收 ──────────────────────────────────────────────────────────────

    def on_tool_event(self, event: dict | None) -> None:
        """由 agent 适配层的 tool_event_callback 调用。

        event 的具体结构由各 agent 决定，此处只做透传和记录。
        """
        if event is None:
            return
        self._events.append({"ts": time.monotonic() - self._started_at, "data": event})

        evt_type = event.get("type") or event.get("name") or "unknown"
        logger.debug(
            "[AgentEventReactor:%s] #%d source=%s type=%s",
            self.run_id, len(self._events), self.source, evt_type,
        )

        # TODO: 触发策略
        # 目前事件全部积累到 on_complete()，不做实时触发
        # 未来在此处根据：事件类型 / 距上次角色发声时间 / 任务重要性 做判断

    # ── 任务完成 ──────────────────────────────────────────────────────────────

    def on_complete(self, final_text: str) -> None:
        """任务结束后调用。打印摘要，后续会注入主 LLM context。"""
        elapsed = time.monotonic() - self._started_at
        n = len(self._events)
        logger.info(
            "[AgentEventReactor:%s] done: source=%s events=%d elapsed=%.1fs result=%s...",
            self.run_id, self.source, n, elapsed, final_text[:80],
        )

        if n == 0:
            return

        # TODO: 上下文摘要生成
        # summary = _build_summary(self.task, self._events, final_text)
        # inject_system_addon(summary)   ← patch 进主 LLM system context

        # TODO: 触发角色播报
        # if self._notify_character and _should_notify(self._events, elapsed):
        #     self._notify_character(summary)

    # ── 只读属性 ──────────────────────────────────────────────────────────────

    @property
    def events(self) -> list[dict]:
        """返回已收集的事件列表（只读副本）。"""
        return list(self._events)

    @property
    def event_count(self) -> int:
        return len(self._events)

    def tool_names(self) -> list[str]:
        """本次任务调用过的工具类型列表（去重，保序）。"""
        seen: list[str] = []
        for evt in self._events:
            d = evt["data"]
            name = d.get("type") or d.get("name") or "unknown"
            if name not in seen:
                seen.append(name)
        return seen
