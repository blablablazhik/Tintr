"""Tintr desktop app: pywebview window (Edge WebView2) around the HTML/CSS/JS UI in tintr/web/static."""

from __future__ import annotations

import os
import sys

from tintr import APP_NAME, __version__


def main() -> int:
    import webview

    from tintr.web import server
    from tintr.web.api import Api

    api = Api()
    _srv, port = server.start(api._allowed_paths)
    api._port = port
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
