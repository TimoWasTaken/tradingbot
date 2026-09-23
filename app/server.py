"""Local web UI for the desktop edition: a tiny HTTP server on 127.0.0.1 that serves app/ui/index.html and a JSON API.

The browser (Edge or Chrome in app mode) is the window. Nothing is reachable from other machines."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import core
from bot.notify import Notifier

UI_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "ui"
if not UI_DIR.exists():
    UI_DIR = Path(__file__).resolve().parent / "ui"


class State:
    def __init__(self):
        self.cfg = core.load_config()
        self.log = core.LogBuffer()
        self.worker: core.Worker | None = None
        self.last_ping = time.time()
        self.lock = threading.Lock()
        self.quit_event = threading.Event()
        self.tray_ok = False

    def running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def start(self) -> tuple[bool, str]:
        st = core.license_status(self.cfg)
        if not st["ok"]:
            return False, "expired"
        if not [w for w in self.cfg["watches"] if w.get("enabled", True)]:
            return False, "no_watches"
        if self.running():
            return True, ""
        core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        self.worker = core.Worker(self.cfg, self.log)
        self.worker.start()
        self.log("Bevakningen startad.")
        return True, ""

    def stop(self) -> None:
        if self.worker:
            self.worker.stop_event.set()
            self.worker = None
            self.log("Bevakningen pausad.")

    def restart_if_running(self) -> None:
        if self.running():
            self.stop()
            self.start()

    def snapshot(self) -> dict:
        w = self.worker if self.running() else None
        return {
            "version": core.VERSION, "running": bool(w), "checking": bool(w and w.checking),
            "next_check_s": max(0, int(w.next_check - time.time())) if w and w.next_check else None,
            "license": core.license_status(self.cfg), "onboarded": bool(self.cfg.get("onboarded")),
            "settings": {k: self.cfg.get(k) for k in ("ntfy_topic", "poll_minutes", "discount_alert_pct", "location", "language", "autostart",
                                                    "tradera_app_id", "tradera_app_key", "apify_token", "license_email", "license_key")},
            "regions": core.REGIONS, "watches": self.cfg["watches"], "deals": core.read_deals(), "log": self.log.tail(),
            "sales_url": core.SALES_URL, "tray": self.tray_ok,
        }


STATE = State()


def open_window(url: str) -> None:
    """Edge or Chrome in app mode (a plain window without tabs); the default browser as a fallback."""
    candidates = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    for exe in candidates:
        if Path(exe).exists():
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1040,760"], close_fds=True)
                return
            except Exception:  # noqa: BLE001
                continue
    webbrowser.open(url)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:      # keep the console quiet
        pass

    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = (UI_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
        elif path == "/api/state":
            STATE.last_ping = time.time()
            self._json(STATE.snapshot())
        elif path == "/api/ping":
            self._json({"ok": True, "version": core.VERSION})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()
        cfg = STATE.cfg
        with STATE.lock:
            if path == "/api/settings":
                for k in ("ntfy_topic", "location", "language", "tradera_app_id", "tradera_app_key", "apify_token"):
                    if k in body:
                        cfg[k] = str(body[k]).strip()
                if "poll_minutes" in body:
                    try:
                        cfg["poll_minutes"] = max(3, int(float(body["poll_minutes"])))
                    except (TypeError, ValueError):
                        pass
                if "discount_alert_pct" in body:
                    try:
                        cfg["discount_alert_pct"] = float(body["discount_alert_pct"])
                    except (TypeError, ValueError):
                        pass
                if "autostart" in body:
                    cfg["autostart"] = bool(body["autostart"])
                    core.set_autostart(cfg["autostart"])
                if "onboarded" in body:
                    cfg["onboarded"] = bool(body["onboarded"])
                core.save_config(cfg)
                STATE.restart_if_running()
                self._json({"ok": True})
            elif path == "/api/watch/toggle":
                for w in cfg["watches"]:
                    if w["name"] == body.get("name"):
                        w["enabled"] = bool(body.get("enabled", True))
                core.save_config(cfg)
                STATE.restart_if_running()
                self._json({"ok": True})
            elif path == "/api/watch/save":
                w = body.get("watch") or {}
                name, q = str(w.get("name", "")).strip(), str(w.get("q", "")).strip()
                if not name or not q:
                    self._json({"ok": False, "error": "need_name"})
                    return
                new = {"name": name, "q": q, "enabled": True, "icon": str(w.get("icon") or "🔎"), "blurb": str(w.get("blurb") or "")}
                for key in ("min_price", "max_price", "alert_below", "discount_alert_pct"):
                    v = str(w.get(key, "")).strip().replace(" ", "")
                    if v:
                        try:
                            new[key] = float(v)
                        except ValueError:
                            pass
                new["min_price"] = new.get("min_price", 0)
                new["pages"] = max(1, min(3, int(w.get("pages") or 1)))
                for key in ("must_words", "exclude_words"):
                    raw = w.get(key, "")
                    words = [x.strip() for x in (raw if isinstance(raw, list) else str(raw).split(",")) if x.strip()]
                    if key == "exclude_words" and not words:
                        words = list(core.COMMON_EXCLUDE)
                    if words:
                        new[key] = words
                url = str(w.get("marketplace_url", "")).strip()
                if url.startswith("http"):
                    new["marketplace_url"] = url
                original = str(body.get("original") or "")
                idx = next((i for i, x in enumerate(cfg["watches"]) if x["name"] == original), None)
                if idx is None:
                    if any(x["name"] == name for x in cfg["watches"]):
                        self._json({"ok": False, "error": "exists"})
                        return
                    cfg["watches"].append(new)
                else:
                    new["builtin"] = cfg["watches"][idx].get("builtin", False)
                    new["enabled"] = cfg["watches"][idx].get("enabled", True)
                    cfg["watches"][idx] = new
                core.save_config(cfg)
                STATE.restart_if_running()
                self._json({"ok": True})
            elif path == "/api/watch/delete":
                cfg["watches"] = [x for x in cfg["watches"] if x["name"] != body.get("name")]
                core.save_config(cfg)
                STATE.restart_if_running()
                self._json({"ok": True})
            elif path == "/api/watch/reset":
                cfg["watches"] = core.default_watches()
                core.save_config(cfg)
                STATE.restart_if_running()
                self._json({"ok": True})
            elif path == "/api/test_push":
                topic = str(body.get("topic") or cfg.get("ntfy_topic") or "").strip()
                ok = bool(topic) and Notifier(topic=topic).send("Blocket Deal Finder", "Klart! Så här kommer fynden att se ut. 🎉", tags=["tada"])
                self._json({"ok": ok})
            elif path == "/api/start":
                ok, why = STATE.start()
                self._json({"ok": ok, "error": why})
            elif path == "/api/stop":
                STATE.stop()
                self._json({"ok": True})
            elif path == "/api/check_now":
                if not STATE.running():
                    ok, why = STATE.start()
                    self._json({"ok": ok, "error": why})
                    return
                STATE.worker.wake.set()
                self._json({"ok": True})
            elif path == "/api/activate":
                email, key = str(body.get("email", "")).strip(), str(body.get("key", "")).strip().upper()
                import licensing as lic
                if not lic.valid(email, key):
                    self._json({"ok": False, "error": "bad_key"})
                    return
                cfg["license_email"], cfg["license_key"] = email, key
                core.save_config(cfg)
                self._json({"ok": True})
            elif path == "/api/open":
                url = str(body.get("url", ""))
                if url.startswith("http"):
                    webbrowser.open(url)
                self._json({"ok": True})
            elif path == "/api/open_data":
                import os
                core.DATA_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(core.DATA_DIR)  # noqa: S606
                self._json({"ok": True})
            elif path == "/api/quit":
                STATE.stop()
                self._json({"ok": True})
                STATE.quit_event.set()
            else:
                self._json({"error": "not found"}, 404)


def serve(port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
