"""Malformed root diagnostics expose schema facts, never authored payload text."""
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.cooperative_provider_loop import CooperativeProviderLoop, LoopConflict


@pytest.mark.parametrize("payload,root_type,keys,action_type", [
    (["PRIVATE_VALUE"], "list", [], "missing"),
    (None, "NoneType", [], "missing"),
    ({"action":"PRIVATE_VALUE", "intent":"PRIVATE_INTENT", "source":"PRIVATE_SOURCE"},
        "dict", ["action", "intent", "source"], "str"),
    ({"say":["PRIVATE_VALUE"], "action":{"op":"PRIVATE_OP", "target":"PRIVATE_TARGET"}},
        "dict", ["action", "say"], "dict"),
    ({"say":"PRIVATE_VALUE", "action":{"op":"work"},
        "PRIVATE_KEY":"PRIVATE_CONTENT"},
        "dict", ["action", "say"], "dict"),
    ({"say":False, "action":{"PRIVATE_ACTION_KEY":"PRIVATE_ACTION_CONTENT"}},
        "dict", ["action", "say"], "dict"),
])
async def test_invalid_root_logs_only_bounded_shape(caplog, payload, root_type, keys, action_type):
    query = AsyncMock(return_value=json.dumps(payload))
    loop = CooperativeProviderLoop(SimpleNamespace(get_manifest=lambda _:None), query,
        Mock(side_effect=AssertionError("no execution")), provider="fake", context_requirements={})
    with caplog.at_level(logging.WARNING, logger="server.cooperative_provider_loop"):
        with pytest.raises(LoopConflict, match="^invalid coordination shape$"):
            await loop._decide({"source":"user", "text":"PRIVATE_USER_TEXT"})
    query.assert_awaited_once()
    records = [record for record in caplog.records if record.message.startswith("rejected coordination root shape: ")]
    assert len(records) == 1
    assert "PRIVATE" not in records[0].getMessage()
    shape = json.loads(records[0].getMessage().split(": ", 1)[1])
    assert shape["root_type"] == root_type
    assert shape["keys"] == keys
    assert shape["action_type"] == action_type
    if isinstance(payload, dict):
        assert shape["field_types"] == {key:type(payload[key]).__name__ for key in keys}
        assert shape["other_key_count"] == len(payload) - len(keys)
    if isinstance(payload, dict) and isinstance(payload.get("action"), dict):
        assert "PRIVATE_ACTION_KEY" not in shape["action_shape"]["keys"]
    assert loop.children == {} and loop.receipts == {}


@pytest.mark.parametrize("source,raw", [
    ("user", '{"say":"普通の返答","action":null}'),
    ("host_receipt", 'PRIVATE_RAW_TEXT {"action":"quoted only"}'),
])
async def test_valid_user_and_text_presentation_do_not_emit_shape_warning(caplog, source, raw):
    loop = CooperativeProviderLoop(SimpleNamespace(get_manifest=lambda _:None),
        AsyncMock(return_value=raw), Mock(), provider="fake", context_requirements={})
    with caplog.at_level(logging.WARNING, logger="server.cooperative_provider_loop"):
        result = await loop._decide({"source":source, "text":"fact"})
    assert result["action"] is None
    assert not any(record.message.startswith("rejected coordination root shape: ") for record in caplog.records)


async def test_null_action_discards_extra_root_data_without_logging_values(caplog):
    raw = json.dumps({"action":None, "say":"普通の返答",
        "type":"json_object", "nested":{"action":{"op":"work"},
            "provider":"PRIVATE_PROVIDER", "task":"PRIVATE_TASK"}})
    loop = CooperativeProviderLoop(SimpleNamespace(get_manifest=lambda _:None),
        AsyncMock(return_value=raw), Mock(side_effect=AssertionError("no execution")),
        provider="fake", context_requirements={})
    with caplog.at_level(logging.WARNING, logger="server.cooperative_provider_loop"):
        result = await loop._decide({"source":"user", "text":"chat"})
    assert result == {"action":None, "say":"普通の返答"}
    assert loop.children == {} and loop.receipts == {}
    assert not any(record.message.startswith("rejected coordination root shape: ")
        for record in caplog.records)


async def test_complete_parser_rejects_conflicting_duplicate_action_even_if_last_is_null():
    raw = ('{"action":null,"say":"普通の返答",'
        '"action":{"op":"work"},"action":null}')
    loop = CooperativeProviderLoop(SimpleNamespace(get_manifest=lambda _:None),
        AsyncMock(return_value=raw), Mock(side_effect=AssertionError("no execution")),
        provider="fake", context_requirements={})
    with pytest.raises(LoopConflict, match="^invalid coordination JSON$") as raised:
        await loop._decide({"source":"user", "text":"chat"})
    assert isinstance(raised.value.__cause__, ValueError)
    assert loop.children == {} and loop.receipts == {}
