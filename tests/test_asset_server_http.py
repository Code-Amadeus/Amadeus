from __future__ import annotations

import email.utils
import http.client
import json
import os
import socket
from contextlib import closing
from pathlib import Path

import pytest

from render.server import AssetServer


_BODY = b"synthetic texture bytes"


@pytest.fixture
def asset_server(tmp_path: Path):
    root = tmp_path / "project"
    mounted = tmp_path / "external"
    root.mkdir()
    mounted.mkdir()
    for directory in (root, mounted):
        (directory / "frame.ktx2").write_bytes(_BODY)
        (directory / ".env").write_bytes(b"private")
        (directory / "api_key.txt").write_bytes(b"private")
        (directory / "nested").mkdir()
    (tmp_path / "outside.dat").write_bytes(b"outside boundary")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    server = AssetServer(root, start_port=port)
    server.mount_static("/external", mounted)
    server.set_dynamic_route("/info", lambda: {"ready": True})

    def fail():
        raise ValueError("synthetic route failure")

    server.set_dynamic_route("/failure", fail)
    server.start()
    try:
        yield server, root, mounted
    finally:
        server.stop()


def _request(server: AssetServer, method: str, path: str, headers=None):
    with closing(http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)) as connection:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, {name.lower(): value for name, value in response.getheaders()}, body


def test_root_and_mounted_static_files_send_revalidation_metadata(asset_server) -> None:
    server, _, _ = asset_server
    for path in ("/frame.ktx2", "/external/frame.ktx2"):
        for query in ("", "?v=untrusted-version"):
            status, headers, body = _request(server, "GET", path + query)
            assert status == 200
            assert body == _BODY
            assert headers["cache-control"] == "no-cache"
            assert headers["etag"].startswith('W/"')
            assert email.utils.parsedate_to_datetime(headers["last-modified"])
            assert headers["content-length"] == str(len(_BODY))
            assert headers["x-content-type-options"] == "nosniff"
            assert "access-control-allow-origin" not in headers

            status, head_headers, body = _request(server, "HEAD", path + query)
            assert status == 200
            assert body == b""
            for name in ("etag", "last-modified", "content-length", "content-type"):
                assert head_headers[name] == headers[name]


def test_etag_and_last_modified_revalidate_root_and_mount_without_a_body(asset_server) -> None:
    server, _, _ = asset_server
    for path in ("/frame.ktx2", "/external/frame.ktx2"):
        _, headers, _ = _request(server, "GET", path)
        etag = headers["etag"]
        for condition in (etag, etag.removeprefix("W/"), f'"other", {etag}', "*"):
            status, response_headers, body = _request(
                server, "GET", path, {"If-None-Match": condition}
            )
            assert status == 304
            assert body == b""
            assert response_headers["etag"] == etag
            assert response_headers["last-modified"] == headers["last-modified"]
            assert response_headers["cache-control"] == "no-cache"

        status, _, body = _request(server, "HEAD", path, {"If-None-Match": etag})
        assert status == 304
        assert body == b""
        status, _, body = _request(
            server, "GET", path, {"If-Modified-Since": headers["last-modified"]}
        )
        assert status == 304
        assert body == b""


def test_same_url_replacement_invalidates_etag_within_one_http_date_second(asset_server) -> None:
    server, root, mounted = asset_server
    first_mtime = 1_700_000_000_100_000_000
    second_mtime = 1_700_000_000_200_000_000
    replacement = b"replacement frame bytes"
    assert len(replacement) == len(_BODY)
    for path, directory in (("/frame.ktx2", root), ("/external/frame.ktx2", mounted)):
        target = directory / "frame.ktx2"
        os.utime(target, ns=(first_mtime, first_mtime))
        _, before, _ = _request(server, "GET", path)
        target.write_bytes(replacement)
        os.utime(target, ns=(second_mtime, second_mtime))

        status, after, body = _request(
            server,
            "GET",
            path,
            {"If-None-Match": before["etag"], "If-Modified-Since": before["last-modified"]},
        )
        assert status == 200
        assert body == replacement
        assert after["etag"] != before["etag"]
        assert after["last-modified"] == before["last-modified"]


def test_unmatched_etag_takes_precedence_over_date_and_bad_dates_are_ignored(asset_server) -> None:
    server, _, _ = asset_server
    future = email.utils.formatdate(2_000_000_000, usegmt=True)
    for path in ("/frame.ktx2", "/external/frame.ktx2"):
        for condition in (
            {"If-None-Match": 'W/"different"', "If-Modified-Since": future},
            {"If-Modified-Since": "not a date"},
        ):
            status, _, body = _request(server, "GET", path, condition)
            assert status == 200
            assert body == _BODY


def test_http11_connection_reuses_socket_across_framed_responses(asset_server) -> None:
    server, _, _ = asset_server
    with closing(http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)) as connection:
        connection.request("GET", "/external/frame.ktx2")
        response = connection.getresponse()
        assert response.version == 11
        assert response.read() == _BODY
        etag = response.getheader("ETag")
        original_socket = connection.sock
        assert original_socket is not None

        requests = (
            ("HEAD", "/frame.ktx2", {}, 200, b""),
            ("OPTIONS", "/frame.ktx2", {}, 204, b""),
            ("GET", "/info", {}, 200, {"ready": True}),
            ("GET", "/failure", {}, 500, b""),
            ("GET", "/external/frame.ktx2", {"If-None-Match": etag}, 304, b""),
            ("GET", "/external/frame.ktx2", {}, 200, _BODY),
        )
        for method, path, headers, expected_status, expected_body in requests:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            assert response.status == expected_status
            body = response.read()
            assert (json.loads(body) if isinstance(expected_body, dict) else body) == expected_body
            assert connection.sock is original_socket


def test_stop_closes_an_idle_persistent_connection(asset_server) -> None:
    server, _, _ = asset_server
    with closing(http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)) as connection:
        connection.request("GET", "/frame.ktx2")
        response = connection.getresponse()
        assert response.version == 11
        assert response.read() == _BODY
        client_socket = connection.sock
        assert client_socket is not None

        server.stop()
        assert client_socket.recv(1) == b""


@pytest.mark.parametrize("truncate", (False, True))
def test_file_changes_after_metadata_do_not_cross_response_boundary(
    asset_server, monkeypatch, truncate: bool
) -> None:
    server, root, _ = asset_server
    target = root / "frame.ktx2"
    original_fstat = os.fstat
    changed = False

    def stat_then_change(descriptor):
        nonlocal changed
        metadata = original_fstat(descriptor)
        if not changed:
            changed = True
            target.write_bytes(b"short" if truncate else _BODY + b"new trailing bytes")
        return metadata

    # Change the real file immediately after the response metadata snapshot,
    # without relying on socket buffering or a race with the request thread.
    monkeypatch.setattr(os, "fstat", stat_then_change)
    with closing(http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)) as connection:
        connection.request("GET", "/frame.ktx2")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Length") == str(len(_BODY))
        assert changed
        if truncate:
            with pytest.raises(http.client.IncompleteRead) as incomplete:
                response.read()
            assert incomplete.value.partial == b"short"
            assert connection.sock.recv(1) == b""
        else:
            assert response.read() == _BODY
            connection.request("GET", "/info")
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {"ready": True}


def test_static_security_boundaries_apply_to_get_head_and_options(asset_server) -> None:
    server, _, _ = asset_server
    for method in ("GET", "HEAD"):
        for path in (
            "/.env", "/api_key.txt", "/nested/", "/%2e%2e/outside.dat",
            "/external/.env", "/external/api_key.txt", "/external/nested/",
            "/external/%2e%2e/outside.dat", "/external/%2e%2e%5coutside.dat",
        ):
            status, _, body = _request(server, method, path)
            assert status == 404, (method, path)
            assert b"private" not in body
            assert b"outside boundary" not in body
    for method in ("GET", "HEAD", "OPTIONS"):
        status, _, body = _request(
            server, method, "/frame.ktx2", {"Host": "attacker.example"}
        )
        assert status == 421
        assert _BODY not in body
