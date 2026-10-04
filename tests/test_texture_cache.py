from contextlib import closing
import hashlib
import http.client
import json
import os
import socket
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from render.server import AssetServer
from render.texture_cache import KTX2, MAX_BODY_BYTES, MIME, TextureDiskCache
import render.texture_cache as cache_module
import render.server as server_module


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


def test_identical_publication_is_successful_while_reading_and_without_free_disk_space(cache_fixture, monkeypatch):
    cache, source, vendor = cache_fixture
    key, _, _ = identify(cache, source)
    body = container(key)
    cache.publish(key, body)
    other = TextureDiskCache(vendor, cache.root)
    identify(other, source)
    monkeypatch.setattr(cache_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))

    def unexpected_replace(*args):
        pytest.fail("Identical bytes must not be replaced")

    monkeypatch.setattr(os, "replace", unexpected_replace)
    stream, _ = cache.open(key)
    with stream:
        cache.publish(key, body)
        other.publish(key, body)
        assert stream.read() == body
    assert cache._bytes == other._bytes == len(body)
    assert not list(cache.root.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows denies replacement while a normal reader holds the file")
def test_windows_sharing_conflict_preserves_cache_and_later_publication_recovers(cache_fixture):
    cache, source, _ = cache_fixture
    key, _, _ = identify(cache, source)
    original = container(key)
    replacement = container(key, payload=b"new-data")
    cache.publish(key, original)
    stream, _ = cache.open(key)
    with stream:
        with pytest.raises(PermissionError):
            cache.publish(key, replacement)
        assert stream.read() == original
        assert cache._entries[key] == cache._bytes == len(original)
    cache.publish(key, replacement)
    assert (cache.root / (key + ".bc7")).read_bytes() == replacement
    assert cache._entries[key] == cache._bytes == len(replacement)


def test_locked_eviction_remains_indexed_and_can_be_reclaimed_later(cache_fixture, monkeypatch):
    cache, source, _ = cache_fixture
    key, _, _ = identify(cache, source)
    body = container(key)
    cache.publish(key, body)
    second = source.with_name("second.ktx2")
    second.write_bytes(ktx(b"second"))
    key2, _, _ = identify(cache, second)
    cache.publish(key2, container(key2))
    cache.max_bytes = len(body)
    unlink = Path.unlink

    def blocked(path, *args, **kwargs):
        if path == cache.root / (key + ".bc7"):
            raise PermissionError("reader holds this file")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked)
    cache._prune(0)
    assert list(cache._entries) == [key]
    assert cache._bytes == len(body)
    assert (cache.root / (key + ".bc7")).is_file()
    assert not (cache.root / (key2 + ".bc7")).exists()
    cache._prune(1)  # Nothing can be reclaimed; a finite pass preserves accounting.
    assert list(cache._entries) == [key] and cache._bytes == len(body)
    monkeypatch.setattr(Path, "unlink", unlink)
    cache._prune(1)
    assert not cache._entries and cache._bytes == 0


def test_unreclaimable_quota_does_not_forget_existing_replacement_target(cache_fixture, monkeypatch):
    cache, source, _ = cache_fixture
    key, _, _ = identify(cache, source)
    body = container(key)
    cache.publish(key, body)
    second = source.with_name("second.ktx2")
    second.write_bytes(ktx(b"second"))
    key2, _, _ = identify(cache, second)
    cache.publish(key2, container(key2))
    cache.max_bytes = cache._bytes
    unlink = Path.unlink

    def blocked(path, *args, **kwargs):
        if path.suffix == ".bc7":
            raise PermissionError("reader holds this file")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked)
    with pytest.raises(OSError, match="could not be reclaimed"):
        cache.publish(key, container(key, payload=b"larger" * 10))
    assert cache._bytes == sum(cache._entries.values()) == sum(p.stat().st_size for p in cache.root.glob("*.bc7"))
    assert (cache.root / (key + ".bc7")).read_bytes() == body
    assert not list(cache.root.glob("*.tmp"))


def test_identical_cross_process_winner_makes_replace_conflict_successful(cache_fixture, monkeypatch):
    cache, source, _ = cache_fixture
    key, _, _ = identify(cache, source)
    body = container(key)

    def competing_publication(temporary, target):
        target.write_bytes(body)
        raise PermissionError("another publisher's reader holds the winner")

    monkeypatch.setattr(os, "replace", competing_publication)
    cache.publish(key, body)
    assert cache._bytes == cache._entries[key] == len(body)
    assert not list(cache.root.glob("*.tmp"))


@pytest.mark.parametrize("after_temporary", [False, True])
def test_disk_reserve_counts_temporary_space_and_recovers_later(cache_fixture, monkeypatch, after_temporary):
    cache, source, _ = cache_fixture
    key, _, _ = identify(cache, source)
    body = container(key)
    calls = []

    def space(_):
        calls.append(1)
        free = cache.min_free_bytes + len(body) if after_temporary and len(calls) == 1 else cache.min_free_bytes - 1
        return SimpleNamespace(free=free)

    monkeypatch.setattr(cache_module.shutil, "disk_usage", space)
    with pytest.raises(OSError, match="free-space reserve"):
        cache.publish(key, body)
    assert not cache._entries and cache._bytes == 0
    assert not list(cache.root.glob("*.tmp"))
    assert not (cache.root / (key + ".bc7")).exists()
    monkeypatch.setattr(cache_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=cache.min_free_bytes + len(body)))
    cache.publish(key, body)
    assert (cache.root / (key + ".bc7")).read_bytes() == body


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


def test_asset_server_recovers_from_temporary_cache_initialization_failure(tmp_path, monkeypatch):
    server = AssetServer(tmp_path, texture_cache=True)
    now, attempts = [100.0], []
    ready = object()

    def create_cache(_):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("directory temporarily unavailable")
        return ready

    monkeypatch.setattr(server_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cache_module, "TextureDiskCache", create_cache)
    assert server._get_texture_cache() is None
    assert server._get_texture_cache() is None
    assert len(attempts) == 1
    now[0] += 30
    assert server._get_texture_cache() is ready
    assert len(attempts) == 2


def test_http_low_space_is_temporary_and_later_write_succeeds(server, monkeypatch):
    server, cache, _ = server
    _, info, _ = request(server, "GET", "/frame.ktx2", {"Accept": MIME})
    key = info["X-Amadeus-BC7-Key"]
    headers = {"Origin": f"http://127.0.0.1:{server.port}", "X-Amadeus-Cache-Token": info["X-Amadeus-Cache-Token"]}
    body = container(key)
    monkeypatch.setattr(cache_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    assert request(server, "POST", "/_texture-cache/" + key, headers, body)[0] == 503
    assert request(server, "GET", "/frame.ktx2")[0] == 200
    monkeypatch.setattr(cache_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=cache.min_free_bytes + len(body)))
    assert request(server, "POST", "/_texture-cache/" + key, headers, body)[0] == 204


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
