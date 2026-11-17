"""Tintr desktop app: pywebview window (Edge WebView2) around the HTML/CSS/JS UI in tintr/web/static.

`run.py --serve` is the browser mode: no window, the same UI is served to any browser on the network
(e.g. a laptop over Tailscale) while the work itself runs on this machine.
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys

from tintr import APP_NAME, __version__


def _args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="Tintr", description=f"{APP_NAME} {__version__}")
    p.add_argument("--serve", action="store_true", help="browser mode: serve the UI over the network, no window")
    p.add_argument("--host", default="0.0.0.0", help="browser mode: address to listen on (default: all)")
    p.add_argument("--port", type=int, default=8765, help="browser mode: port (default: 8765)")
    return p.parse_args(argv)


def _addresses(host: str) -> list[str]:
    if host not in ("0.0.0.0", "", "::"):
        return [host]
    out = [socket.gethostname()]
    try:
        out += sorted({ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)})
    except OSError:
        pass
    return out


def serve(host: str, port: int) -> int:
    import threading

    from tintr.web import server
    from tintr.web.api import Api

    for stream in (sys.stdout, sys.stderr):  # Cyrillic must not crash a cp1252 console or a pipe
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    api = Api()
    api._remote = True  # noqa: SLF001
    # a fixed token (TINTR_TOKEN) keeps the laptop's link valid across restarts
    token = os.environ.get("TINTR_TOKEN") or secrets.token_urlsafe(18)
    srv, port = server.start(api._allowed_paths, api=api, host=host, port=port, token=token)  # noqa: SLF001
    print(f"{APP_NAME} {__version__} — браузерний режим. Відкрийте на ноуті одне з посилань:", flush=True)
    for a in _addresses(host):
        print(f"  http://{a}:{port}/?t={token}", flush=True)
    print("Зупинити: Ctrl+C", flush=True)
    stop = threading.Event()
    try:
        while not stop.wait(0.5):  # short waits keep Ctrl+C responsive on Windows
            pass
    except KeyboardInterrupt:
        pass
    if api._busy:  # noqa: SLF001
        api._pl.cancel.set()  # noqa: SLF001
    srv.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    a = _args(sys.argv[1:] if argv is None else argv)
    if a.serve:
        return serve(a.host, a.port)

    import webview

    from tintr.web import server
    from tintr.web.api import Api

    api = Api()
    _srv, port = server.start(api._allowed_paths)
    window = webview.create_window(
        f"{APP_NAME} {__version__}", f"http://127.0.0.1:{port}/index.html", js_api=api,
        width=1500, height=950, min_size=(1100, 720), background_color="#021019",
    )
    api._window = window

    def on_closing():
        if api._busy:  # noqa: SLF001
            api._pl.cancel.set()
        return True

    window.events.closing += on_closing
    icon = os.path.join(os.path.dirname(__file__), "web", "static", "tintr.ico")  # window + taskbar icon
    webview.start(debug=os.environ.get("TINTR_DEBUG") == "1", private_mode=False,
                  icon=icon if os.path.isfile(icon) else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
