"""A loopback tile-cache proxy: Leaflet points its tile layer at
`http://127.0.0.1:<port>/tiles/{z}/{x}/{y}.png`, this serves from the
on-disk cache when it can and falls back to fetching from OpenStreetMap
(with a real User-Agent, per OSM's tile usage policy) when it can't. Online
the first time through a spot, cached and offline-capable every time after.

Plain stdlib http.server, threaded, bound to loopback only. Nothing here
is ever reachable off this machine.
"""

from __future__ import annotations

import base64
import functools
import re
import threading
import urllib.error
import urllib.request
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from warmap import config

# A 1x1 fully transparent PNG, served whenever a tile is neither cached
# nor reachable, so a missing connection degrades to "blank map square"
# instead of a broken-image icon or a crashed request.
PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg=="
)

_TILE_PATH_RE = re.compile(r"^/tiles/(\d{1,3})/(\d{1,10})/(\d{1,10})\.png$")


class TileCacheProxy:
    """Owns the cache dir + fetch policy. `_fetch_upstream` is a separate
    method (not inlined into fetch_tile) specifically so tests can
    `mock.patch.object` it and exercise the cache-hit/cache-miss/offline
    paths without ever touching the real network."""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        user_agent: str = config.TILE_USER_AGENT,
        upstream_template: str = config.TILE_UPSTREAM,
        timeout: float = 6.0,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else config.TILE_CACHE_DIR
        self.user_agent = user_agent
        self.upstream_template = upstream_template
        self.timeout = timeout
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server is not None else 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> int:
        """Bind to an ephemeral loopback port and start serving in a daemon
        thread. Idempotent-ish: calling start() twice without stop() in
        between just leaks the first server, so callers own one instance
        per app lifetime (see app.py)."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        handler = functools.partial(_TileRequestHandler, proxy=self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="warmap-tileproxy")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    # --- tile fetch/cache, independent of the HTTP layer -----------------

    def _cache_path(self, z: str, x: str, y: str) -> Path:
        return self.cache_dir / str(z) / str(x) / f"{y}.png"

    def fetch_tile(self, z: str, x: str, y: str) -> bytes:
        cache_path = self._cache_path(z, x, y)
        if cache_path.exists():
            try:
                return cache_path.read_bytes()
            except OSError:
                pass  # cache read failed, fall through and try the network
        try:
            data = self._fetch_upstream(z, x, y)
        except (urllib.error.URLError, HTTPException, OSError, TimeoutError, ValueError):
            return PLACEHOLDER_PNG
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
        except OSError:
            pass  # caching is best-effort; the fetched tile is still good
        return data

    def _fetch_upstream(self, z: str, x: str, y: str) -> bytes:
        url = self.upstream_template.format(z=z, x=x, y=y)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise urllib.error.URLError(f"tile fetch failed: HTTP {status}")
            return resp.read()


class _TileRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, proxy: TileCacheProxy, **kwargs):
        self.proxy = proxy
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        # The map abandons pending tile requests the moment it pans or zooms
        # away (panning around the camera overlay does this constantly), so a
        # write to a socket the browser already closed is normal, not a fault.
        # Those (BrokenPipe/ConnectionReset) are swallowed silently; anything
        # else still surfaces. Without this the console fills with tracebacks
        # for tiles nobody's waiting for any more.
        try:
            path = urlsplit(self.path).path
            match = _TILE_PATH_RE.match(path)
            if not match:
                self.send_response(404)
                self.end_headers()
                return
            z, x, y = match.group(1), match.group(2), match.group(3)
            try:
                data = self.proxy.fetch_tile(z, x, y)
            except Exception:  # noqa: BLE001 - upstream fetch failed; tell the client, don't crash the thread
                self.send_response(502)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # keep stdout clean, this is a personal tool, not a service
