"""Local HTTP server for the web UI: static files + project images (with server-side thumbnails).

Only files under the allowed roots (the UI folder, the project's _tintr folder, chosen references)
are served. Desktop mode: 127.0.0.1 only, the UI talks to Python through pywebview.

Browser mode (`api` given): the UI runs in any browser on the network. It calls the Api at
POST /api/<method> (JSON list of args) and gets its events from GET /events (server-sent events).
Every request needs the token: `?t=<token>` once (turned into a SameSite=Strict cookie), then the cookie.
"""

from __future__ import annotations

import hmac
import io
import json
import mimetypes
import queue
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image

STATIC = Path(__file__).parent / "static"
COOKIE = "tintr_t"
MAX_BODY = 64 << 20  # an uploaded reference image fits easily


class _Hub:
    """Fan-out of API events to every open /events stream."""

    def __init__(self):
        self._qs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def publish(self, msg: str) -> None:
        with self._lock:
            for q in self._qs:
                q.put(msg)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._qs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._qs.discard(q)


class _Thumbs:
    def __init__(self, cap: int = 400):
        self.cap = cap
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: Path, w: int) -> bytes:
        key = (str(path), path.stat().st_mtime_ns, w)
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        im = Image.open(path)
        im.load()
        if im.width > w:
            im.thumbnail((w, w * 4), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=1)
        data = buf.getvalue()
        with self._lock:
            self._d[key] = data
            while len(self._d) > self.cap:
                self._d.popitem(last=False)
        return data


def start(allowed: Callable[[], list[Path]], api=None, host: str = "127.0.0.1", port: int = 0,
          token: str = "") -> tuple[ThreadingHTTPServer, int]:
    """Serve on `port` (0 = a free one); `allowed()` returns the folders/files images may come from.

    With `api` the server runs in browser mode (see the module doc) and `token` is required."""
    thumbs = _Thumbs()
    hub = _Hub()
    if api is not None:
        assert token, "browser mode needs a token"
        api._listeners.append(hub.publish)  # noqa: SLF001

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # quiet
            pass

        def _send(self, code: int, data: bytes, ctype: str, cache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=31536000, immutable" if cache else "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _token_ok(self, u) -> bool:
            if api is None:
                return True
            c = SimpleCookie(self.headers.get("Cookie", ""))
            got = c[COOKIE].value if COOKIE in c else ""
            return hmac.compare_digest(got.encode(), token.encode())

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = hub.subscribe()
            try:
                self.wfile.write(b": hi\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(b"data: " + msg.encode() + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")  # keeps proxies and dead peers honest
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                hub.unsubscribe(q)

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            try:
                name = u.path[len("/api/"):] if u.path.startswith("/api/") else ""
                if api is None or not self._token_ok(u):
                    return self._send(403, b"forbidden", "text/plain")
                fn = getattr(api, name, None) if name and not name.startswith("_") else None
                if not callable(fn):
                    return self._send(404, b"no such method", "text/plain")
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._send(413, b"too large", "text/plain")
                args = json.loads(self.rfile.read(n) or b"[]")
                res = fn(*args) if isinstance(args, list) else fn()
                return self._send(200, json.dumps(res, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                try:
                    self._send(500, f"{type(e).__name__}: {e}".encode(), "text/plain; charset=utf-8")
                except Exception:  # noqa: BLE001
                    pass

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            try:
                if api is not None:
                    t = parse_qs(u.query).get("t", [""])[0]
                    if t and hmac.compare_digest(t.encode(), token.encode()):
                        self.send_response(302)  # token → cookie, and out of the address bar
                        self.send_header("Set-Cookie", f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
                        self.send_header("Location", "/")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if not self._token_ok(u):
                        return self._send(403, "Немає доступу: відкрийте посилання з токеном, яке Tintr показав у терміналі."
                                          .encode(), "text/plain; charset=utf-8")
                    if u.path == "/events":
                        return self._events()
                if u.path == "/f":
                    q = parse_qs(u.query)
                    p = Path(unquote(q.get("p", [""])[0])).resolve()
                    roots = [r.resolve() for r in allowed()]
                    if not p.is_file() or not any(p == r or r in p.parents for r in roots):
                        return self._send(404, b"not found", "text/plain")
                    w = int(q.get("w", ["0"])[0] or 0)
                    if w:
                        return self._send(200, thumbs.get(p, w), "image/png", cache=True)
                    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                    return self._send(200, p.read_bytes(), ctype, cache=True)
                rel = u.path.lstrip("/") or "index.html"
                p = (STATIC / rel).resolve()
                if STATIC.resolve() not in p.parents or not p.is_file():
                    return self._send(404, b"not found", "text/plain")
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                if p.suffix == ".js":
                    ctype = "text/javascript"
                data = p.read_bytes()
                if api is not None and p.name == "index.html":  # tells app.js to talk HTTP instead of pywebview
                    data = data.replace(b'<script src="app.js">', b'<script>window.TINTR_REMOTE = true;</script><script src="app.js">')
                return self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                try:
                    self._send(500, str(e).encode(), "text/plain")
                except Exception:  # noqa: BLE001
                    pass

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
