"""Every displayed JSON example is one complete cooperative response."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.cooperative_provider_loop import COORDINATION_CONTRACT, CooperativeProviderLoop


def complete_examples():
    examples = []
    position = 0
    while (position := COORDINATION_CONTRACT.find("{", position)) >= 0:
        value, consumed = json.JSONDecoder().raw_decode(COORDINATION_CONTRACT[position:])
        examples.append(value)
        position += consumed
    return examples


def test_contract_examples_have_one_consistent_root_and_keep_batch_spans():
    examples = complete_examples()
    assert len(examples) == 4
    assert all(list(example) == ["action", "say"] and isinstance(example["say"], str)
        for example in examples)
    assert examples[0]["action"] is None
    assert examples[1]["action"] == {"op":"work", "intent":"execute"}
    batches = [example["action"]["actions"] for example in examples[2:]]
    assert [[item["op"] for item in batch] for batch in batches] == [
        ["work", "report"], ["work", "auip_after_work"]]
    assert [item["source"] for item in batches[0]] == [
        "把 alpha.md 的标题改成‘修订版’", "顺便告诉我 beta.md 对应任务现在什么状态。"]
    assert [item["source"] for item in batches[1]] == [
        "创建一个计数器应用", "完成后打开它，我们一起试一下。"]


@pytest.mark.parametrize("example", complete_examples())
async def test_each_complete_example_passes_the_unchanged_user_parser(example):
    action = example["action"]
    text = ("；".join(item["source"] for item in action["actions"])
        if isinstance(action, dict) and action.get("op") == "batch" else "依頼を確認して。")
    query = AsyncMock(return_value=json.dumps(example, ensure_ascii=False))
    loop = CooperativeProviderLoop(SimpleNamespace(get_manifest=lambda _:None), query,
        Mock(side_effect=AssertionError("no execution")), provider="fake", context_requirements={})
    value = await loop._decide({"source":"user", "text":text})
    assert value["say"] == example["say"]
    if isinstance(action, dict) and action.get("op") == "batch":
        assert [{key:item for key,item in row.items() if key not in {"source_start", "source_end"}}
            for row in value["action"]["actions"]] == action["actions"]
    else:
        assert value["action"] == action
    assert loop.children == {} and loop.receipts == {}
