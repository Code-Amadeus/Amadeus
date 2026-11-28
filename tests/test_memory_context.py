"""Packing preserves source meaning/version independently of candidate ranking."""
from types import SimpleNamespace

import pytest

from eidema import lookup as memory_context
from eidema.lookup import serialize_reference, estimate_reply_tokens, pack_reference


def candidate(text, identity="memory", version=1, condition=None):
    return SimpleNamespace(memory_id=identity,row_version=version,text=text,kind="fact",
        condition=condition,scope=SimpleNamespace(kind="character"))


def test_165_cjk_characters_fit_named_estimate_and_hard_payload_ceiling():
    text = ("我不能吃花生，只有医生确认安全后才会改变这个限制。" * 7)[:165]
    item = candidate(text)
    result, budget, omitted = pack_reference([item])
    assert result["entries"][0]["text"] == text and len(text) == 165
    assert budget["unit"] == "estimated_reply_tokens"
    assert budget["used"] == estimate_reply_tokens(serialize_reference(result)) <= 512
    assert len(serialize_reference(result).encode("utf-8")) <= 4096 and omitted == 0


def test_oversized_entry_does_not_consume_final_item_slots_or_rewrite_text():
    items = [candidate("长" * 5000,"large"),
        *[candidate(f"small relevant fact {index}",str(index)) for index in range(5)]]
    result, _, omitted = pack_reference(items)
    assert len(result["entries"]) == 4 and omitted == 2
    assert [entry["text"] for entry in result["entries"]] == [f"small relevant fact {index}" for index in range(4)]
    assert items[0].text == "长" * 5000


def test_whole_entry_keeps_qualification_condition_and_negation():
    text = "只有下大雨时才坐公交；其他时候不坐公交。"
    item = candidate(text,condition="Only when it rains heavily")
    result, _, _ = pack_reference([item])
    entry = result["entries"][0]
    assert entry["text"] == text and entry["condition"] == item.condition
    assert "memory_id" not in entry and "row_version" not in entry
    assert item.text == text


def test_host_exact_count_contract_counts_entire_compact_reference_envelope():
    item = candidate("exact count contract")
    seen = []
    def count(value):
        seen.append(value)
        return len(value)
    result, budget, _ = pack_reference([item],token_count=count,count_method="test-host-codepoint-tokenizer")
    assert budget["unit"] == "reply_model_tokens"
    assert budget["method"] == "test-host-codepoint-tokenizer"
    assert budget["used"] == len(serialize_reference(result))
    assert serialize_reference(result) in seen


def test_exact_count_contract_requires_named_counter_and_valid_count():
    with pytest.raises(ValueError,match="method_required"):
        pack_reference([],token_count=len)
    with pytest.raises(ValueError,match="invalid_reply_token_count"):
        pack_reference([],token_count=lambda text:True,count_method="invalid")


@pytest.mark.parametrize("exact", [False, True])
def test_over_byte_payload_skips_token_work_and_keeps_later_whole_entry_and_accounting(monkeypatch, exact):
    byte_limit = 256
    oversized = candidate("长" * 32768, condition="条件" * 2048)
    fitting = candidate("A fitting fact.", condition="Only on weekdays.")
    seen = []
    expected_counter = len if exact else estimate_reply_tokens
    def bounded_counter(value):
        assert len(value.encode("utf-8")) <= byte_limit
        seen.append(value)
        return expected_counter(value)
    options = {"token_count":bounded_counter,"count_method":"test-codepoint-tokenizer"} if exact else {}
    if not exact:
        monkeypatch.setattr(memory_context, "estimate_reply_tokens", bounded_counter)
    payload, budget, omitted = pack_reference([oversized, fitting], byte_limit=byte_limit, **options)
    assert payload["entries"] == [{"text":fitting.text, "scope":"character", "kind":"fact",
        "condition":fitting.condition}]
    assert omitted == 1 and oversized.text == "长" * 32768
    serialized = serialize_reference(payload)
    assert seen and all(value == serialized for value in seen)
    assert budget["used"] == expected_counter(serialized)
    assert budget["used_utf8_bytes"] == len(serialized.encode("utf-8")) <= byte_limit
    assert budget["unit"] == ("reply_model_tokens" if exact else "estimated_reply_tokens")
    assert budget["method"] == ("test-codepoint-tokenizer" if exact else "ascii_3_nonascii_2_nonbmp_4_estimate")


@pytest.mark.parametrize("exact", [False, True])
def test_byte_fitting_payload_still_obeys_token_limit_and_final_accounting(exact):
    token_heavy = candidate("长" * 250)
    fitting = candidate("small fact")
    options = {"token_count":lambda value:len(value.encode("utf-8")),
        "count_method":"test-utf8-byte-tokenizer"} if exact else {}
    payload, budget, omitted = pack_reference([token_heavy, fitting], **options)
    assert len(serialize_reference({"authority":"user_reference_only", "entries":[{
        "text":token_heavy.text,"scope":"character","kind":"fact"}]}).encode("utf-8")) < 4096
    assert [entry["text"] for entry in payload["entries"]] == [fitting.text]
    assert omitted == 1
    serialized = serialize_reference(payload)
    expected = len(serialized.encode("utf-8")) if exact else estimate_reply_tokens(serialized)
    assert budget["used"] == expected <= budget["limit"]
    assert budget["used_utf8_bytes"] == len(serialized.encode("utf-8"))


def test_invalid_counter_still_fails_for_byte_fitting_candidate():
    with pytest.raises(ValueError, match="invalid_reply_token_count"):
        pack_reference([candidate("fits")], token_count=lambda value:-1, count_method="invalid")


def test_explicit_whole_entry_selection_matches_exact_provenance_budget():
    items = [candidate("oversized" * 2000, "large", condition="keep whole"),
        *[candidate(f"fact {index}", str(index), condition="weekday only") for index in range(10)]]
    selected = []
    payload, budget, omitted = pack_reference(items, token_count=lambda text:len(text) // 3,
        count_method="synthetic-exact-counter", token_limit=1024, byte_limit=8192,
        item_limit=8, selected=selected)
    assert [entry["text"] for entry in payload["entries"]] == [item.text for item in selected]
    assert len(selected) == 8 and omitted == 3
    assert all(entry["condition"] == "weekday only" for entry in payload["entries"])
    assert budget["unit"] == "reply_model_tokens" and budget["used"] <= 1024
