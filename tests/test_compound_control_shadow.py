"""Shared exact-source clauses stay ordered, unique, and non-overlapping."""
from server.compound_control import parse_decomposition_reply


def test_parser_requires_exact_ordered_non_overlapping_source_clauses() -> None:
    source = "把 alpha.txt 改成 one-updated；顺便告诉我 beta.txt 现在什么状态。"
    clauses = parse_decomposition_reply(
        '{"clauses":["顺便告诉我 beta.txt 现在什么状态","把 alpha.txt 改成 one-updated"]}',
        source_user_text=source,
    )
    assert [clause.text for clause in clauses] == [
        "把 alpha.txt 改成 one-updated",
        "顺便告诉我 beta.txt 现在什么状态",
    ]
    for reply in (
        '{"clauses":["修改 alpha.txt"]}',
        '{"clauses":["alpha.txt","alpha.txt"]}',
        '{"clauses":["alpha.txt","alpha.txt 改成 one-updated"]}',
        '{"clauses":["a","b","c","d"]}',
        '{"clauses":[" alpha.txt"]}',
        '{"clauses":["alpha.txt"],"extra":true}',
    ):
        try:
            parse_decomposition_reply(reply, source_user_text=source)
        except ValueError:
            pass
        else:
            raise AssertionError(reply)
