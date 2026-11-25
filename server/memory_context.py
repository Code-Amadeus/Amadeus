"""Host reference packing, independent of retrieval ranking and model admission."""
import json
import math


def serialize_reference(value):
    # Match _decide's model-frame serializer, including its default spaces.
    return json.dumps(value, ensure_ascii=False)


def estimate_reply_tokens(value):
    """A bounded practical estimate, not an exact closed-model tokenizer.

    ASCII uses one estimated token per three codepoints; non-ASCII uses two
    per codepoint (four for non-BMP symbols). The separate byte ceiling bounds
    payload size even when an unknown reply tokenizer differs from this method.
    """
    ascii_count = sum(ord(char) < 128 for char in value)
    return math.ceil(ascii_count / 3) + sum(
        4 if ord(char) > 0xffff else 2 for char in value if ord(char) >= 128)


def pack_reference(candidates, *, token_count=None, count_method=None,
                   token_limit=512, byte_limit=4096, item_limit=4, selected=None):
    """Pack whole, already revalidated candidates; never truncate source meaning."""
    exact = token_count is not None
    counter = token_count or estimate_reply_tokens
    method = count_method if exact else "ascii_3_nonascii_2_nonbmp_4_estimate"
    if exact and (not isinstance(method, str) or not method.strip()):
        raise ValueError("reply_token_count_method_required")
    entries = []
    payload = {"authority":"user_reference_only", "entries":entries}

    def count_tokens(text):
        count = counter(text)
        if type(count) is not int or count < 0:
            raise ValueError("invalid_reply_token_count")
        return count

    omitted = 0
    for candidate in candidates:
        entry = {"text":candidate.text,
            "scope":candidate.scope.kind,"kind":candidate.kind}
        if candidate.condition:
            entry["condition"] = candidate.condition
        if len(entries) >= item_limit:
            omitted += 1
            continue
        entries.append(entry)
        text = serialize_reference(payload)
        # An already oversized whole entry cannot fit regardless of token count.
        # Reject it before invoking an exact tokenizer or the codepoint estimate.
        if len(text.encode("utf-8")) > byte_limit or count_tokens(text) > token_limit:
            entries.pop()
            omitted += 1
        elif selected is not None:
            selected.append(candidate)
    text = serialize_reference(payload)
    count, size = count_tokens(text), len(text.encode("utf-8"))
    budget = {"unit":"reply_model_tokens" if exact else "estimated_reply_tokens",
        "method":method,"limit":token_limit,"used":count,
        "hard_utf8_bytes":byte_limit,"used_utf8_bytes":size}
    return payload, budget, omitted
