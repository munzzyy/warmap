"""The phone bridge: a local HTTP server that hands this machine's captures to
the mobile app over your own network.

Everything else in warmap is loopback-only or offline. This is the one piece
that accepts a connection from another device, so it is built to be boring and
tight:

* **Off by default.** Nothing starts it but an explicit action, the desktop's
  "Send to phone" dialog or `warmap serve`. There is no autostart and no
  daemon.
* **Token in the path.** Every URL lives under `/s/<token>/`, where the token
  is 32 URL-safe random characters minted per run. Without it the server
  answers 404 to everything, so a stranger on the same network who guesses the
  port still gets nothing. Compared with `hmac.compare_digest`, and checked on
  the request line before a single header is read, so a stranger cannot make
  this machine buffer anything.
* **Bounded.** At most MAX_CONNECTIONS connections at a time; past that the
  socket is closed unanswered. One phone needs a handful.
* **Read-only.** GET and HEAD only; every other method is refused. The phone
  can look at your captures, it can never change them, and there is no path
  from a request into the store, the filesystem outside the app, or a parser.
* **No path traversal.** Static files resolve against a fixed directory and
  the resolved path must still be inside it, or it is a 404. No directory
  listing, no symlink following out.
* **LAN only.** It binds to the machine's own address on your network, which
  is what a phone can reach, not the public internet. Behind a normal router
  that is exactly the set of devices already on your wifi. The dialog says so
  in those words, because "it's on your network" is a real consideration and
  the user should make it deliberately.

The data itself is served from a `DataSource` the caller supplies, so this
module knows nothing about Qt, the store, or how records were parsed. It
serves whatever the running app hands it, which keeps the phone in sync with
what the desktop is actually showing.
"""

from __future__ import annotations

import functools
import gzip
import hmac
import io
import json
import mimetypes
import re
import secrets
import socket
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from warmap import config

# How many connections may be open at once. A phone loading the app opens a
# few in parallel for tiles; a stranger opening hundreds gets the door shut.
MAX_CONNECTIONS = 32

# Responses smaller than this aren't worth compressing: the CPU and the
# header overhead cost more than the bytes saved.
GZIP_MIN_BYTES = 1024

# The deepest zoom OpenStreetMap serves. A request past it names no real tile,
# so it is refused before it can become an upstream fetch or a cache write.
MAX_TILE_ZOOM = 19

# The camera set is the big payload (about 9 MB of compact JSON, a couple of
# MB gzipped). Everything else is small.
_TILE_PATH_RE = re.compile(r"^tiles/(\d{1,3})/(\d{1,10})/(\d{1,10})\.png$")

# Static files the app shell is allowed to ask for, mapped to where they live.
# An explicit table rather than "serve this directory": it is the simplest
# thing that cannot be talked into serving something else.
_WEBAPP_FILES = {
    "": "index.html",
    "index.html": "index.html",
    "app.js": "app.js",
    "app.css": "app.css",
    "sw.js": "sw.js",
    "manifest.webmanifest": "manifest.webmanifest",
    "icon-192.png": "icon-192.png",
    "icon-512.png": "icon-512.png",
    "icon-maskable.png": "icon-maskable.png",
    "apple-touch-icon.png": "apple-touch-icon.png",
}

# Shared with the desktop map: one renderer, two hosts. Served out of
# warmap/web/ rather than duplicated into the webapp, so a fix to the map is a
# fix in both places and they can't drift.
_SHARED_WEB_FILES = {
    "map.js": "map.js",
    "map.css": "map.css",
}


@dataclass
class DataSource:
    """What the server is allowed to publish.

    Each field is a zero-argument callable returning a JSON-able object, so
    the server always reflects what the app holds *now* rather than a snapshot
    taken when it started. Defaults return empty payloads, which makes the
    server safe to construct and test with no app around it.
    """

    records: Callable[[], dict] = lambda: {"type": "FeatureCollection", "features": []}
    cameras: Callable[[], dict] = lambda: {"cameras": [], "attribution": ""}
    tracks: Callable[[], Optional[dict]] = lambda: None
    session: Callable[[], dict] = dict


@dataclass
class ServerInfo:
    """Where the phone should point, and what to show the human."""

    url: str
    host: str
    port: int
    token: str
    lan_ip: str = ""
    extra: dict = field(default_factory=dict)


def lan_ip() -> str:
    """This machine's address on the local network.

    Opens a UDP socket toward a public address and reads back which local
    interface the routing table picked. No packet is actually sent (UDP
    connect is just a bind), and it needs no network to answer. It is asking
    the kernel a routing question, not the internet a real one.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class _BoundedServer(ThreadingHTTPServer):
    """One thread per connection, up to a fixed number. The accept loop never
    blocks; a connection past the limit is closed at once."""

    daemon_threads = True

    def __init__(self, *args, max_connections: int = MAX_CONNECTIONS, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class WarmapServer:
    """Serves the mobile app and the current dataset to devices on your
    network. Start it, hand the URL to a phone, stop it when you're done."""

    def __init__(
        self,
        data: Optional[DataSource] = None,
        tile_proxy=None,
        host: str = "0.0.0.0",
        port: int = 0,
        token: Optional[str] = None,
        webapp_dir: Optional[Path] = None,
        web_dir: Optional[Path] = None,
    ):
        self.data = data or DataSource()
        self.tile_proxy = tile_proxy
        self.host = host
        self._requested_port = port
        # 32 URL-safe characters, 192 bits. Long enough that guessing is not
        # a thing, short enough to survive being put in a QR code.
        self.token = token or secrets.token_urlsafe(24)
        self.webapp_dir = Path(webapp_dir) if webapp_dir else config.WEBAPP_DIR
        self.web_dir = Path(web_dir) if web_dir else config.WEB_DIR
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # Cheap request accounting so the desktop dialog can show that the
        # phone actually connected, rather than leaving you guessing.
        self.hits = 0
        self.last_client = ""
        self._hits_lock = threading.Lock()

    # --- lifecycle --------------------------------------------------------

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server is not None else 0

    @property
    def running(self) -> bool:
        return self._server is not None

    def info(self) -> ServerInfo:
        ip = lan_ip() if self.host in ("0.0.0.0", "") else self.host
        return ServerInfo(
            url=f"http://{ip}:{self.port}/s/{self.token}/",
            host=self.host, port=self.port, token=self.token, lan_ip=ip,
        )

    def start(self) -> ServerInfo:
        handler = functools.partial(_Handler, app=self)
        self._server = _BoundedServer((self.host, self._requested_port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="warmap-server"
        )
        self._thread.start()
        return self.info()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    # --- routing ----------------------------------------------------------

    def token_ok(self, candidate: str) -> bool:
        """Constant-time token check that answers False for anything odd.

        Compared as bytes, not str: `hmac.compare_digest` refuses to compare
        strings containing non-ASCII characters and raises TypeError instead.
        The request line arrives latin-1 decoded, so a single high byte in the
        URL used to raise on this exact line (the one pre-authentication path
        every request crosses), which dropped the connection with no response
        at all and printed a traceback, instead of the 404 that everything
        else gets. Encoding first makes an unusual token just a wrong one.
        """
        try:
            supplied = candidate.encode("utf-8", "surrogateescape")
        except (UnicodeError, AttributeError):
            return False
        return hmac.compare_digest(supplied, self.token.encode("utf-8"))

    def static_bytes(self, name: str) -> Optional[tuple[bytes, str]]:
        """(body, content-type) for an allowed static file, or None.

        Only names in the two tables above resolve at all, and the resolved
        path has to still be inside the directory it came from: belt and
        braces, since the tables already contain no user input.
        """
        if name in _WEBAPP_FILES:
            base, filename = self.webapp_dir, _WEBAPP_FILES[name]
        elif name in _SHARED_WEB_FILES:
            base, filename = self.web_dir, _SHARED_WEB_FILES[name]
        elif name.startswith("vendor/"):
            base, filename = self.web_dir, name
        else:
            return None

        try:
            path = (base / filename).resolve()
            if not path.is_file() or base.resolve() not in path.parents:
                return None
            body = path.read_bytes()
        except OSError:
            return None
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".webmanifest":
            ctype = "application/manifest+json"
        elif path.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        return body, ctype


class _Handler(BaseHTTPRequestHandler):
    server_version = "warmap"
    sys_version = ""
    # Drop a connection that opens and then dawdles. Each one holds a thread
    # for as long as it stays open, so without this a handful of sockets that
    # never finish sending a request (deliberately or because a phone walked
    # out of wifi range mid-request) would sit there indefinitely.
    timeout = 30
    # Keep-alive: a phone loading the app pulls the shell, the data and a lot
    # of tiles, and a fresh TCP handshake for each is the slowest part of that
    # over wifi. Safe here because every response goes out with an accurate
    # Content-Length (see _send), and the timeout above bounds how long an
    # idle connection may hold its thread.
    protocol_version = "HTTP/1.1"

    def handle_one_request(self) -> None:
        """The stdlib loop, with the token checked on the request line before
        the headers are read. A stranger's request is answered and closed
        after one line; only a holder of the token gets this machine to parse
        anything further."""
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self._request_line_carries_token(self.raw_requestline):
                self.wfile.write(
                    b"HTTP/1.1 404 Not Found\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-Length: 9\r\n"
                    b"Connection: close\r\n\r\n"
                    b"Not found"
                )
                self.wfile.flush()
                self.close_connection = True
                return
            if not self.parse_request():
                return
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self.send_error(HTTPStatus.NOT_IMPLEMENTED,
                                f"Unsupported method ({self.command!r})")
                return
            method()
            self.wfile.flush()
        except (TimeoutError, OSError):
            # A timed-out or reset connection is an expected end, not a fault.
            self.close_connection = True

    def _request_line_carries_token(self, raw: bytes) -> bool:
        words = raw.decode("iso-8859-1").rstrip("\r\n").split()
        if len(words) < 2:
            return False
        parts = urlsplit(words[1]).path.strip("/").split("/", 2)
        return len(parts) >= 2 and parts[0] == "s" and self.app.token_ok(parts[1])

    def __init__(self, *args, app: WarmapServer, **kwargs):
        self.app = app
        super().__init__(*args, **kwargs)

    # --- helpers ----------------------------------------------------------

    def _send(self, body: bytes, ctype: str, status: int = 200,
              cache: str = "no-store") -> None:
        # The phone asks for the same 2 MB camera payload on every load, so
        # gzip is worth it here in a way it never was for the tile proxy.
        encoding = None
        if (len(body) >= GZIP_MIN_BYTES
                and "gzip" in self.headers.get("Accept-Encoding", "")):
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
                gz.write(body)
            body = buf.getvalue()
            encoding = "gzip"

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Cache-Control", cache)
        # This app is served to one phone on one network and embeds nothing.
        # These cost nothing and close the obvious doors.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload) -> None:
        self._send(json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _not_found(self) -> None:
        # Same answer for "wrong token" and "no such file", so the response
        # never tells anyone whether they guessed a real token.
        self._send(b"Not found", "text/plain; charset=utf-8", status=404)

    # --- methods ----------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        # A phone that pans the map abandons tile requests constantly; a write
        # to a socket that already went away is normal, not a fault.
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _refuse(self) -> None:
        self._send(b"warmap serves read-only", "text/plain; charset=utf-8", status=405)

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

    def _route(self) -> None:
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/", 2)

        # Everything lives under /s/<token>/. Anything else is a stranger.
        if len(parts) < 2 or parts[0] != "s" or not self.app.token_ok(parts[1]):
            self._not_found()
            return
        rest = parts[2] if len(parts) > 2 else ""

        with self.app._hits_lock:
            self.app.hits += 1
            self.app.last_client = self.client_address[0]

        tile = _TILE_PATH_RE.match(rest)
        if tile:
            self._serve_tile(*tile.groups())
            return

        if rest.startswith("api/"):
            self._serve_api(rest[4:])
            return

        found = self.app.static_bytes(rest)
        if found is None:
            self._not_found()
            return
        body, ctype = found
        # The shell may change between runs (a warmap update), and the dataset
        # is served fresh anyway, so nothing here is cached by the browser.
        # The service worker is what makes the app fast offline, not HTTP
        # caching.
        self._send(body, ctype)

    def _serve_api(self, name: str) -> None:
        data = self.app.data
        try:
            if name == "session":
                self._json(data.session())
            elif name == "records":
                self._json(data.records())
            elif name == "cameras":
                self._json(data.cameras())
            elif name == "tracks":
                self._json(data.tracks())
            else:
                self._not_found()
        except Exception:  # noqa: BLE001, a provider that raises must not kill the thread
            self._send(b'{"error":"unavailable"}', "application/json; charset=utf-8",
                       status=503)

    def _serve_tile(self, z: str, x: str, y: str) -> None:
        proxy = self.app.tile_proxy
        if proxy is None:
            self._not_found()
            return
        # A tile request is the one thing a phone can ask for that makes this
        # machine reach out to OpenStreetMap and write a file. The path regex
        # already pins the host and keeps z/x/y numeric, so there is no way to
        # steer the upstream URL, but "0/0/0" through "40/999999/999999" is
        # still a lot of requests someone could sit in a loop issuing, which
        # would put the user's IP through OSM's rate limiter and grow the
        # cache without end. Only coordinates that name a real tile get
        # through; the rest never reach the network or the disk.
        try:
            zoom, tx, ty = int(z), int(x), int(y)
        except ValueError:
            self._not_found()
            return
        if not (0 <= zoom <= MAX_TILE_ZOOM):
            self._not_found()
            return
        limit = 1 << zoom
        if not (0 <= tx < limit and 0 <= ty < limit):
            self._not_found()
            return

        try:
            body = proxy.fetch_tile(z, x, y)
        except Exception:  # noqa: BLE001
            self._send(b"", "image/png", status=502)
            return
        # Tiles are immutable for practical purposes; let the phone keep them.
        self._send(body, "image/png", cache="public, max-age=604800")

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # personal tool, not a service, keep stdout clean
