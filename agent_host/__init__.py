# -*- coding: utf-8 -*-
"""agent_host — Amadeus agent 托管层

职责边界：
  - 接收来自各 agent 适配器的原生事件
  - 将事件翻译为 Amadeus 解释层可消费的结构
  - 管理任务上下文摘要、触发策略、system context 注入

不包含任何 agent 实现逻辑——那是适配层（openclaw/）的工作。
"""
