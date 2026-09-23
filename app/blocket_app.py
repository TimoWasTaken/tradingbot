"""Blocket Deal Finder, desktop edition 2.0: the launcher.

Starts the local web UI (app/server.py) on 127.0.0.1, opens it in Edge or Chrome as an app window, and keeps
running in the background with a tray icon so the watch loop survives the window being closed.
Running the .exe again while it is already running just re-opens the window.
Built into a single .exe with PyInstaller (build_app.bat). Settings live in %APPDATA%\\BlocketDealFinder."""
from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core  # noqa: E402
import server  # noqa: E402

PORT_FILE = core.DATA_DIR / "port.txt"


def existing_instance() -> str | None:
    try:
        port = int(PORT_FILE.read_text().strip())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            if r.status == 200:
                return f"http://127.0.0.1:{port}/"
    except Exception:  # noqa: BLE001
        return None
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tray(url: str, quit_event: threading.Event) -> None:
    """System tray icon: Open / Quit. Falls back silently if pystray or Pillow is missing."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        quit_event.wait()
        return
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(31, 111, 235, 255))
    d.ellipse((22, 18, 42, 38), outline=(255, 255, 255, 255), width=5)
    d.line((38, 36, 50, 50), fill=(255, 255, 255, 255), width=6)

    def on_open(_icon, _item):
        server.open_window(url)

    def on_quit(icon, _item):
        quit_event.set()
        icon.stop()

    icon = pystray.Icon("BlocketDealFinder", img, "Blocket Deal Finder",
                        menu=pystray.Menu(pystray.MenuItem("Öppna", on_open, default=True), pystray.MenuItem("Avsluta", on_quit)))
    server.STATE.tray_ok = True
    threading.Thread(target=lambda: (quit_event.wait(), icon.stop()), daemon=True).start()
    icon.run()


def main() -> int:
    background = "--background" in sys.argv
    url = existing_instance()
    if url:
        if not background:
            server.open_window(url)
        return 0
    port = free_port()
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    PORT_FILE.write_text(str(port))
    httpd = server.serve(port)
    url = f"http://127.0.0.1:{port}/"
    if server.STATE.cfg.get("onboarded"):        # once set up, the watch runs whenever the program runs
        server.STATE.start()
    if not background:
        time.sleep(0.3)
        server.open_window(url)
    tray(url, server.STATE.quit_event)
    server.STATE.quit_event.set()
    server.STATE.stop()
    httpd.shutdown()
    try:
        PORT_FILE.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
