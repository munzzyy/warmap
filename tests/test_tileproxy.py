"""Tile cache proxy: cache-hit/miss/offline-fallback paths, the HTTP layer's
path validation, and the User-Agent OSM's tile policy requires.

`_fetch_upstream` is mocked via mock.patch.object on the *instance*
(patching the injection point directly) for the cache-logic tests, so none
of them ever touch the real network. See the lesson in
feedback_mock_patch_object_over_default_args. One lower-level test mocks
urllib.request.urlopen itself to confirm the real Request object carries
the right header.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from unittest import mock

import pytest

from warmap.tileproxy import PLACEHOLDER_PNG, TileCacheProxy

FAKE_TILE_BYTES = b"\x89PNG\r\n\x1a\nFAKE-TILE-DATA"


@pytest.fixture
def proxy(tmp_path):
    p = TileCacheProxy(cache_dir=tmp_path / "tiles")
    yield p
    p.stop()


def test_cache_miss_fetches_and_caches(proxy, tmp_path):
    with mock.patch.object(proxy, "_fetch_upstream", return_value=FAKE_TILE_BYTES) as fetch:
        data = proxy.fetch_tile("5", "10", "12")
    assert data == FAKE_TILE_BYTES
    fetch.assert_called_once_with("5", "10", "12")
    cached_path = tmp_path / "tiles" / "5" / "10" / "12.png"
    assert cached_path.exists()
    assert cached_path.read_bytes() == FAKE_TILE_BYTES


def test_cache_hit_never_calls_upstream(proxy, tmp_path):
    cached_path = tmp_path / "tiles" / "3" / "4" / "5.png"
    cached_path.parent.mkdir(parents=True)
    cached_path.write_bytes(FAKE_TILE_BYTES)

    with mock.patch.object(proxy, "_fetch_upstream") as fetch:
        data = proxy.fetch_tile("3", "4", "5")
    assert data == FAKE_TILE_BYTES
    fetch.assert_not_called()


def test_offline_falls_back_to_placeholder_without_crashing(proxy, tmp_path):
    with mock.patch.object(proxy, "_fetch_upstream", side_effect=urllib.error.URLError("no route")):
        data = proxy.fetch_tile("1", "1", "1")
    assert data == PLACEHOLDER_PNG
    # placeholder must NOT be cached, so a later request should retry the network
    assert not (tmp_path / "tiles" / "1" / "1" / "1.png").exists()


def test_offline_timeout_also_falls_back(proxy):
    with mock.patch.object(proxy, "_fetch_upstream", side_effect=TimeoutError("timed out")):
        assert proxy.fetch_tile("2", "2", "2") == PLACEHOLDER_PNG


def test_a_second_request_for_a_cached_tile_uses_the_cache(proxy, tmp_path):
    with mock.patch.object(proxy, "_fetch_upstream", return_value=FAKE_TILE_BYTES) as fetch:
        proxy.fetch_tile("7", "8", "9")
        proxy.fetch_tile("7", "8", "9")
    fetch.assert_called_once()  # second call hit the on-disk cache, not the network


def test_fetch_upstream_sets_user_agent_header(tmp_path):
    proxy = TileCacheProxy(cache_dir=tmp_path / "tiles", user_agent="warmap/1.0 (test)")
    captured = {}

    class _FakeResponse:
        status = 200

        def read(self):
            return FAKE_TILE_BYTES

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["request"] = req
        return _FakeResponse()

    with mock.patch("warmap.tileproxy.urllib.request.urlopen", side_effect=_fake_urlopen):
        data = proxy._fetch_upstream("1", "2", "3")
    assert data == FAKE_TILE_BYTES
    assert captured["request"].get_header("User-agent") == "warmap/1.0 (test)"
    assert "1" in captured["request"].full_url and "2" in captured["request"].full_url


def test_start_stop_real_http_server(tmp_path):
    proxy = TileCacheProxy(cache_dir=tmp_path / "tiles")
    port = proxy.start()
    try:
        assert port > 0
        with mock.patch.object(proxy, "_fetch_upstream", return_value=FAKE_TILE_BYTES):
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/tiles/4/8/16.png", timeout=5)
            body = resp.read()
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "image/png"
        assert body == FAKE_TILE_BYTES
        cached_path = tmp_path / "tiles" / "4" / "8" / "16.png"
        assert cached_path.exists()
        assert cached_path.read_bytes() == FAKE_TILE_BYTES
    finally:
        proxy.stop()


def test_invalid_tile_path_returns_404(tmp_path):
    proxy = TileCacheProxy(cache_dir=tmp_path / "tiles")
    port = proxy.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/tiles/not-a-number/2/3.png", timeout=5)
        assert exc_info.value.code == 404
    finally:
        proxy.stop()


def test_unrelated_path_returns_404(tmp_path):
    proxy = TileCacheProxy(cache_dir=tmp_path / "tiles")
    port = proxy.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5)
        assert exc_info.value.code == 404
    finally:
        proxy.stop()
