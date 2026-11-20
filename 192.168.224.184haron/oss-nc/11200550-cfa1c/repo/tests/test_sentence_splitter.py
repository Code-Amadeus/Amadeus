from __future__ import annotations

from llm.sentence_splitter import split_stream_buffer_for_first_sentence
from tools.ttfp_test import _build_early_cut_text


def test_japanese_early_cut_waits_for_a_natural_boundary() -> None:
    partial = "何の実験だか気になるけ"

    head, tail, reason = split_stream_buffer_for_first_sentence(partial, 11, "日文")

    assert head == ""
    assert tail == partial
    assert reason == "japanese_wait_boundary"


def test_ttfp_probe_uses_the_production_japanese_boundary() -> None:
    text = "何の実験だか気になるけど、まあいいわ。"

    assert _build_early_cut_text(text, 11) == "何の実験だか気になるけど、"
