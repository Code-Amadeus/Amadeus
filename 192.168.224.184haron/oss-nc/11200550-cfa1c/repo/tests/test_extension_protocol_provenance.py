"""Offline drift gate for the explicitly pinned protocol schema and validator."""
from tools.check_extension_protocol import check_protocol


def test_pinned_protocol_copies_have_not_drifted():
    check_protocol()
