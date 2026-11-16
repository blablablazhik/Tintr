"""Local HTTP server for the web UI: static files + project images (with server-side thumbnails).

Only files under the allowed roots (the UI folder, the project's _tintr folder, chosen references)
are served, on 127.0.0.1 only.
"""

from __future__ import annotations

import io
import mimetypes
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image

STATIC = Path(__file__).parent / "static"


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


def start(allowed: Callable[[], list[Path]]) -> tuple[ThreadingHTTPServer, int]:
    """Serve on a free port; `allowed()` returns the folders/files images may come from."""
    thumbs = _Thumbs()

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

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            try:
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
                return self._send(200, p.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                try:
                    self._send(500, str(e).encode(), "text/plain")
                except Exception:  # noqa: BLE001
                    pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
