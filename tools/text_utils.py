"""
纯文本解析工具集。

本模块所有函数均为无状态纯函数（或无副作用的 async 生成器），
不依赖任何全局变量，可被项目中任意模块安全导入。

包含：
  - TAG_LABEL_PATTERN      VTS/OpenClaw 标签正则
  - _parse_sentence_seq    从 sentence_id 提取序号
  - _compute_text_sha1     文本 SHA1 短摘要（用于去重/缓存 key）
  - _parse_seconds         "1.5s" / "200ms" → float 秒
  - _parse_float_list      逗号分隔浮点列表解析
  - _pair_ids_values       将 id 列表与 value 列表配对为 dict
  - _parse_attr_kv         标签属性串 "k=v k2=v2" → dict
  - parse_tags_and_clean   提取标签动作 + 返回去标签纯文本
  - strip_tags             仅移除标签，不返回动作
  - async_generator_from_sync          同步生成器 → 异步生成器
  - async_generator_from_sync_threaded 阻塞同步生成器 → 异步生成器（后台线程）
"""

import re
import hashlib
import asyncio
from threading import Thread

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 匹配 VTS / OpenClaw 控制标签，例如 [EXPR name=smile] [DELEGATE task="..."]
TAG_LABEL_PATTERN = re.compile(
    r"\[(PARAM|EXPR|HOTKEY|EMO|ANIM|DELEGATE|AUIP)([^\]]*)\]",
    flags=re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 序号 / 摘要工具
# ---------------------------------------------------------------------------

def _parse_sentence_seq(sentence_id: str) -> int:
    """从 'sentence_9_1757506723' 提取序号 9，失败返回极大值以免阻塞排序。"""
    try:
        if not sentence_id:
            return 10 ** 9
        parts = sentence_id.split("_")
        if len(parts) >= 2 and parts[0] == "sentence":
            return int(parts[1])
        return int(parts[1])
    except Exception:
        return 10 ** 9


def _compute_text_sha1(s: str) -> str:
    """返回文本的 SHA1 前 12 位十六进制（用于去重 / 缓存 key）。"""
    try:
        return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:12]
    except Exception:
        return "sha1_err"

# ---------------------------------------------------------------------------
# 数值解析工具
# ---------------------------------------------------------------------------

def _parse_seconds(text: str, default_val: float = 0.0) -> float:
    """
    将 "1.5s" / "200ms" / "1.5" 解析为秒数浮点值。
    解析失败时返回 default_val。
    """
    if not text:
        return default_val
    try:
        s = text.strip().lower()
        if s.endswith("ms"):
            return float(s[:-2]) / 1000.0
        if s.endswith("s"):
            return float(s[:-1])
        return float(s)
    except Exception:
        return default_val


def _parse_float_list(s: str) -> list:
    """将 "0.7,0.2,1.0" 解析为 [0.7, 0.2, 1.0]，解析失败返回 []。"""
    try:
        return [float(x) for x in s.split(",")] if s else []
    except Exception:
        return []


def _pair_ids_values(ids_text: str, values_text: str) -> dict:
    """
    将 id 列表与 value 列表配对。
    若 value 只有一个且 id 有多个，则广播该 value 到所有 id。
    """
    ids  = [x.strip() for x in (ids_text  or "").split(",") if x.strip()]
    vals = _parse_float_list(values_text or "")
    if len(vals) == 1 and len(ids) > 1:
        vals = [vals[0]] * len(ids)
    return {pid: vals[i] for i, pid in enumerate(ids) if i < len(vals)}

# ---------------------------------------------------------------------------
# 标签解析
# ---------------------------------------------------------------------------

def _parse_attr_kv(attr_text: str) -> dict:
    """
    将标签属性串解析为字典。
    例：" id=MouthSmile,BrowInnerUp value=0.7,0.2 dur=2s ease=easeOut "
    →  {"id": "MouthSmile,BrowInnerUp", "value": "0.7,0.2", "dur": "2s", "ease": "easeOut"}
    """
    attrs = {}
    s = re.sub(r"\s+", " ", attr_text.strip())
    if not s:
        return attrs
    for m in re.finditer(r"(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s]+)", s):
        value = m.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        attrs[m.group(1)] = value
    return attrs


def _parse_delegate_attrs(attr_text: str) -> dict:
    """Parse DELEGATE attributes while preserving legacy unquoted task text."""
    attrs = _parse_attr_kv(attr_text)
    if "task" not in attrs:
        tm = re.search(r'task\s*=\s*(.+)', attr_text.strip())
        if tm:
            attrs["task"] = tm.group(1).strip().strip("'\"")
    return attrs


def parse_tags_and_clean(text: str):
    """
    提取文本中的 VTS/OpenClaw 控制标签，返回 (clean_text, actions)。

    action 结构：
        {"type": "EXPR", "attrs": {...}, "raw": "[EXPR ...]"}

    标签类型：PARAM / EXPR / HOTKEY / EMO / ANIM / DELEGATE / AUIP
    """
    actions = []
    if not text:
        return text, actions

    def repl(match: re.Match) -> str:
        tag_type  = match.group(1).upper()
        attr_text = match.group(2) or ""
        attrs = _parse_delegate_attrs(attr_text) if tag_type == "DELEGATE" else _parse_attr_kv(attr_text)
        actions.append({"type": tag_type, "attrs": attrs, "raw": match.group(0)})
        return ""

    clean_text = TAG_LABEL_PATTERN.sub(repl, text)
    return clean_text, actions


def strip_tags(text: str) -> str:
    """仅移除控制标签，不返回动作。"""
    if not text:
        return text
    return TAG_LABEL_PATTERN.sub("", text)

# ---------------------------------------------------------------------------
# 异步生成器适配器
# ---------------------------------------------------------------------------

async def async_generator_from_sync(sync_gen_func):
    """
    将同步生成器函数包装为异步生成器。
    适用于生成器本身不阻塞事件循环的场景（每个 item 生成速度很快）。
    """
    for item in sync_gen_func():
        yield item
        await asyncio.sleep(0)


async def async_generator_from_sync_threaded(sync_gen_func):
    """
    将可能阻塞的同步生成器放到后台线程消费，通过 asyncio.Queue 线程安全回传数据。
    适用于每次 next() 可能耗时较长的同步生成器（如模型推理）。
    """
    loop  = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=8)

    def _worker():
        try:
            for item in sync_gen_func():
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, item)
                except Exception:
                    pass
        except Exception as e:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ("__ERROR__", e))
            except Exception:
                pass
        finally:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ("__DONE__", None))
            except Exception:
                pass

    Thread(target=_worker, daemon=True).start()

    while True:
        item = await queue.get()
        if isinstance(item, tuple) and item[0] == "__DONE__":
            break
        if isinstance(item, tuple) and item[0] == "__ERROR__":
            raise item[1]
        yield item
        await asyncio.sleep(0)
