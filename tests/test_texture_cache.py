from contextlib import closing
import hashlib
import http.client
import json
import os
import socket
import struct

import pytest

from render.server import AssetServer
from render.texture_cache import KTX2, MAX_BODY_BYTES, MIME, TextureDiskCache


def ktx(payload=b"source", width=5, height=7):
    return KTX2 + struct.pack("<9I", 0, 1, width, height, 0, 0, 1, 1, 2) + bytes(32) + payload


def container(key, width=8, height=8, payload=b"compressed-bc7"):
    header = json.dumps({"version": 1, "key": key, "width": width, "height": height,
                         "rawBytes": width * height, "rawSha256": "a" * 64,
                         "compressedSha256": hashlib.sha256(payload).hexdigest()}).encode()
    return struct.pack("<I", len(header)) + header + payload


@pytest.fixture
def cache_fixture(tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "basis_transcoder.js").write_bytes(b"js")
    (vendor / "basis_transcoder.wasm").write_bytes(b"wasm")
    source = tmp_path / "frame.ktx2"
    source.write_bytes(ktx())
    cache = TextureDiskCache(vendor, tmp_path / "cache")
    return cache, source, vendor


def identify(cache, path):
    with path.open("rb") as source:
        identity = cache.identify(source, path, os.fstat(source.fileno()))
        assert source.tell() == 0
        return identity


def test_keys_follow_contents_and_transcoder_not_asset_names(cache_fixture):
    cache, source, vendor = cache_fixture
    first = identify(cache, source)
    assert first[1:] == (8, 8)
    alias = source.with_name("alias.ktx2")
    alias.write_bytes(source.read_bytes())
    assert identify(cache, alias) == first
    source.write_bytes(ktx(b"modified"))
    assert identify(cache, source)[0] != first[0]
    (vendor / "basis_transcoder.wasm").write_bytes(b"new transcoder")
    assert identify(TextureDiskCache(vendor, cache.root), alias)[0] != first[0]


def test_complete_publication_survives_new_session_and_preserves_source(cache_fixture):
    cache, source, vendor = cache_fixture
    original = source.read_bytes()
    key, _, _ = identify(cache, source)
    body = container(key)
    cache.publish(key, body)
    assert source.read_bytes() == original
    assert not list(cache.root.glob("*.tmp"))
    restarted = TextureDiskCache(vendor, cache.root)
    identify(restarted, source)
    stream, _ = restarted.open(key)
    with stream:
        assert stream.read() == body


def test_bad_or_stale_writes_cannot_replace_good_cache(cache_fixture):
    cache, source, _ = cache_fixture
    key, _, _ = identify(cache, source)
    body = container(key)
    cache.publish(key, body)
    for invalid in [b"partial", body[:-1], container(key, 4, 4), container("b" * 64)]:
        with pytest.raises(ValueError):
            cache.publish(key, invalid)
        assert (cache.root / (key + ".bc7")).read_bytes() == body
    source.write_bytes(ktx(b"changed"))
    with pytest.raises(ValueError, match="source changed"):
        cache.publish(key, body)
    with pytest.raises(ValueError, match="Unknown"):
        cache.publish("e" * 64, container("e" * 64))


def test_failed_atomic_replacement_keeps_previous_cache_and_reconciles_quota(cache_fixture, monkeypatch):
    cache, source, _ = cache_fixture
    key, _, _ = identify(cache, source)
    original = container(key)
    cache.publish(key, original)

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        cache.publish(key, container(key, payload=b"replacement"))
    assert (cache.root / (key + ".bc7")).read_bytes() == original
    assert cache._bytes == len(original)
    assert not list(cache.root.glob("*.tmp"))


def test_quota_only_removes_owned_flat_cache_files(cache_fixture):
    cache, source, _ = cache_fixture
    keep = cache.root / "keep.txt"
    keep.write_bytes(b"unrelated")
    directory = cache.root / ("a" * 64 + ".bc7")
    directory.mkdir()
    (directory / "keep.txt").write_bytes(b"nested")
    key1, _, _ = identify(cache, source)
    body1 = container(key1)
    cache.max_bytes = len(body1) + 8
    cache.publish(key1, body1)
    source.write_bytes(ktx(b"second"))
    key2, _, _ = identify(cache, source)
    cache.publish(key2, container(key2))
    assert not (cache.root / (key1 + ".bc7")).exists()
    assert (cache.root / (key2 + ".bc7")).is_file()
    assert keep.read_bytes() == b"unrelated"
    assert (directory / "keep.txt").read_bytes() == b"nested"


@pytest.fixture
def server(cache_fixture):
    cache, source, _ = cache_fixture
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = AssetServer(source.parent, port)
    server._texture_cache = cache
    server.start()
    try:
        yield server, cache, source
    finally:
        server.stop()


def request(server, method, path, headers=None, body=None):
    with closing(http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)) as connection:
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()


def test_http_cold_write_warm_repair_and_original_negotiation(server):
    server, cache, source = server
    status, headers, body = request(server, "GET", "/frame.ktx2", {"Accept": MIME})
    assert status == 200 and body == source.read_bytes()
    key = headers["X-Amadeus-BC7-Key"]
    auth = {"Origin": f"http://127.0.0.1:{server.port}", "X-Amadeus-Cache-Token": headers["X-Amadeus-Cache-Token"]}
    packed = container(key)
    assert request(server, "POST", "/_texture-cache/" + key, auth, packed)[0] == 204
    status, headers, body = request(server, "GET", "/frame.ktx2", {"Accept": MIME})
    assert status == 200 and body == packed and headers["Content-Type"] == MIME
    assert headers["Vary"] == "Accept"
    assert request(server, "GET", "/frame.ktx2", {"Accept": MIME, "If-None-Match": headers["ETag"]})[0] == 304
    assert request(server, "GET", "/frame.ktx2")[2] == source.read_bytes()
    assert request(server, "GET", "/frame.ktx2?source=1", {"Accept": MIME})[2] == source.read_bytes()
    (cache.root / (key + ".bc7")).write_bytes(b"broken payload")
    assert request(server, "POST", "/_texture-cache/" + key, auth, packed)[0] == 204
    assert request(server, "GET", "/frame.ktx2", {"Accept": MIME})[2] == packed


@pytest.mark.parametrize("change", ["no-token", "foreign-origin", "null-origin", "no-origin", "unknown-key", "traversal", "oversize"])
def test_http_write_capability_cannot_escape_cache_or_origin(server, change):
    server, cache, _ = server
    _, info, _ = request(server, "GET", "/frame.ktx2", {"Accept": MIME})
    key = info["X-Amadeus-BC7-Key"]
    headers = {"Origin": f"http://127.0.0.1:{server.port}", "X-Amadeus-Cache-Token": info["X-Amadeus-Cache-Token"]}
    path, body = "/_texture-cache/" + key, container(key)
    if change == "no-token": headers.pop("X-Amadeus-Cache-Token")
    if change == "foreign-origin": headers["Origin"] = "https://example.com"
    if change == "null-origin": headers["Origin"] = "null"
    if change == "no-origin": headers.pop("Origin")
    if change == "unknown-key": path = "/_texture-cache/" + "d" * 64
    if change == "traversal": path = "/_texture-cache/../../frame.ktx2"
    if change == "oversize": headers["Content-Length"] = str(MAX_BODY_BYTES + 1)
    assert request(server, "POST", path, headers, body)[0] in {400, 403, 404}
    assert not list(cache.root.glob("*.bc7"))
