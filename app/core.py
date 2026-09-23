"""Blocket Deal Finder desktop edition: everything that is not the user interface.

Config, default watches, the background worker (Blocket + Tradera + Marketplace through bot/), the deals list,
the log buffer and the licence check. The UI (app/server.py + app/ui/index.html) only talks to this module."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import secrets
import sys
import threading
import time
from collections import deque
from pathlib import Path

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import licensing as lic  # noqa: E402
from bot import blocket as bl  # noqa: E402
from bot.notify import Notifier  # noqa: E402

APP_NAME = "BlocketDealFinder"
VERSION = "2.0"
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = DATA_DIR / "config.json"
JOURNAL_DIR = DATA_DIR / "journal"
SALES_URL = "https://timowastaken.github.io/tradingbot/dealfinder/"

REGIONS = [("", "Hela Sverige"), ("0.300001", "Stockholm"), ("0.300003", "Uppsala"), ("0.300004", "Södermanland"),
           ("0.300005", "Östergötland"), ("0.300006", "Jönköping"), ("0.300007", "Kronoberg"), ("0.300008", "Kalmar"),
           ("0.300009", "Gotland"), ("0.300010", "Blekinge"), ("0.300012", "Skåne"), ("0.300013", "Halland"),
           ("0.300014", "Västra Götaland"), ("0.300017", "Värmland"), ("0.300018", "Örebro"), ("0.300019", "Västmanland"),
           ("0.300020", "Dalarna"), ("0.300021", "Gävleborg"), ("0.300022", "Västernorrland"), ("0.300023", "Jämtland"),
           ("0.300024", "Västerbotten"), ("0.300025", "Norrbotten")]

COMMON_EXCLUDE = ["sökes", "söker", "köpes", "önskas", "defekt", "trasig", "trasigt", "reservdel", "reservdelar", "skadad",
                  "krossad", "bytes", "hyr", "uthyres", "hyres"]


def default_watches() -> list[dict]:
    def w(name, q, mn, mx, icon, blurb, must=None, excl=(), pages=1, alert_below=None, enabled=True):
        d = {"name": name, "q": q, "min_price": mn, "max_price": mx, "exclude_words": COMMON_EXCLUDE + list(excl), "pages": pages,
             "icon": icon, "blurb": blurb, "enabled": enabled, "builtin": True}
        if must:
            d["must_words"] = must
        if alert_below:
            d["alert_below"] = alert_below
        return d
    return [
        w("Festool", "festool", 500, 15000, "🛠️", "Proffsverktyg som håller värdet. Larm under 900 kr eller 30 % under.", excl=["sågblad", "slippapper", "påsar", "tillbehör"], pages=2, alert_below=900),
        w("Hilti", "hilti", 500, 15000, "🔩", "Byggproffsens märke. Larm under 900 kr eller 30 % under.", excl=["spik", "bult", "plugg", "tillbehör"], pages=2, alert_below=900),
        w("Makita 18V", "makita 18v", 800, 6000, "🔋", "Batteriverktyg, säljs snabbt vidare.", must=["makita"], excl=["batteri", "laddare", "bits", "sågblad", "väska", "låda"]),
        w("Bugaboo", "bugaboo", 1500, 9000, "👶", "Fox, Donkey, Cameleon med flera. Bara kompletta vagnar.", must=["fox", "donkey", "cameleon", "buffalo", "bee", "dragonfly", "giraffe", "vagn"],
          excl=["tillbehör", "regnskydd", "åkpåse", "fotsack", "adapter", "liggdel", "sittdel", "turtle", "resesäng", "ram", "chassi",
                "babynest", "ståbräda", "isofix", "bilbarnstol", "base", "klädsel", "sufflett", "korg", "hjul", "väska"], pages=2),
        w("Thule Chariot", "thule chariot", 1500, 12000, "🚴", "Cykelvagnar som alltid hittar köpare.", must=["chariot"],
          excl=["cykelhållare", "takbox", "takräcke", "adapter", "kit", "hjul", "regnskydd", "tillbehör"], pages=2),
        w("Elcykel", "elcykel", 3000, 25000, "🚲", "Kolla alltid ramnummer mot polisen innan köp.", must=["elcykel", "el-cykel", "e-bike"], excl=["batteri", "laddare", "cykelhållare", "kit"], pages=2),
        w("Weber Genesis", "weber genesis", 1000, 12000, "🔥", "Gasolgrillar, säsongsvara med stor marginal på våren.", must=["weber"], excl=["överdrag", "galler", "tillbehör", "kol"]),
        w("Automower", "automower", 2000, 20000, "🌿", "Robotgräsklippare. Billigast på hösten.", must=["automower"], excl=["knivar", "batteri", "laddstation", "kabel", "tillbehör"], pages=2),
        w("Louis Poulsen", "louis poulsen", 800, 15000, "💡", "Designlampor. Larm under 1 200 kr eller 30 % under.", must=["poulsen"], excl=["kopia", "replika", "skärm", "reservglas"], pages=2, alert_below=1200),
        w("String hylla", "string hylla", 500, 15000, "📚", "Klassisk hylla, alltid efterfrågad.", must=["string"], excl=["kopia"], pages=2),
        w("Concept2", "concept2", 3000, 15000, "🏋️", "Roddmaskiner, håller priset i decennier.", must=["concept"], excl=["tillbehör"]),
        w("Canon RF objektiv", "canon rf", 2000, 30000, "📷", "Kameraobjektiv, lätta att skicka.", must=["rf"], excl=["kamerahus", "adapter", "väska", "filter"], pages=2),
    ]


def default_config() -> dict:
    return {"ntfy_topic": "", "poll_minutes": 10, "discount_alert_pct": 30, "location": "", "language": "sv",
            "autostart": False, "onboarded": False, "min_samples": 15, "price_window": 200, "resale_factor": 0.9,
            "request_pause_seconds": 2, "pages": 1, "tradera_app_id": "", "tradera_app_key": "", "apify_token": "",
            "ask_to_sold_factor": 0.6, "selling_cost_pct": 12,
            "tradera": {"enabled": True, "ending_hours": 2, "auction_discount_pct": 40, "min_sold_samples": 5, "history_pages": 8},
            "marketplace": {"enabled": True, "hours_between_runs": 4, "results_per_watch": 8},
            "license_email": "", "license_key": "", "trial_started": "",
            "watches": default_watches()}


def load_config() -> dict:
    cfg = default_config()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            cfg.update(saved)
        except Exception:  # noqa: BLE001
            pass
    changed = False
    if not cfg.get("trial_started"):
        cfg["trial_started"] = dt.date.today().isoformat()
        changed = True
    if not cfg.get("ntfy_topic"):
        cfg["ntfy_topic"] = "fynd-" + secrets.token_hex(4)      # a private channel name nobody can guess
        changed = True
    # older configs: watches without the new fields
    builtin = {w["name"]: w for w in default_watches()}
    for w in cfg["watches"]:
        w.setdefault("enabled", True)
        if w["name"] in builtin:
            w.setdefault("icon", builtin[w["name"]]["icon"])
            w.setdefault("blurb", builtin[w["name"]]["blurb"])
            w.setdefault("builtin", True)
        else:
            w.setdefault("icon", "🔎")
            w.setdefault("blurb", "")
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def license_status(cfg: dict) -> dict:
    if lic.valid(cfg.get("license_email", ""), cfg.get("license_key", "")):
        return {"ok": True, "kind": "licensed", "days": 0, "email": cfg.get("license_email", "")}
    days = lic.trial_days_left(cfg.get("trial_started", ""))
    return {"ok": days > 0, "kind": "trial" if days > 0 else "expired", "days": days, "email": ""}


def set_autostart(enabled: bool) -> None:
    try:
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        link = startup / f"{APP_NAME}.bat"
        exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        if enabled and startup.exists():
            link.write_text(f'@echo off\r\nstart "" "{exe}" --background\r\n', encoding="utf-8")
        elif link.exists():
            link.unlink()
    except Exception:  # noqa: BLE001
        pass


# ---------------- worker ----------------

class Worker(threading.Thread):
    """Runs the watch loop in the background. Only enabled watches are handed to the finder."""

    def __init__(self, cfg: dict, log):
        super().__init__(daemon=True)
        self.cfg = json.loads(json.dumps(cfg))
        self.cfg["watches"] = [w for w in self.cfg["watches"] if w.get("enabled", True)]
        self.log = log
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.next_check = 0.0
        self.last_summary = ""
        self.checking = False

    def run(self) -> None:
        notifier = Notifier(topic=self.cfg.get("ntfy_topic", ""), log=self.log)
        finder = bl.DealFinder(self.cfg, JOURNAL_DIR, notifier, self.log)
        watcher = None
        if self.cfg.get("tradera_app_id") and self.cfg.get("tradera_app_key"):
            try:
                from bot import tradera as tr
                api = tr.TraderaAPI(self.cfg["tradera_app_id"], self.cfg["tradera_app_key"])
                watcher = tr.TraderaWatcher(self.cfg, api, JOURNAL_DIR, notifier, self.log, blocket_reference=finder.asking_reference)
                finder.sold_register = watcher.sold
                self.log("Tradera: på (riktiga försäljningspriser och auktioner som slutar snart).")
            except Exception as e:  # noqa: BLE001
                self.log(f"Tradera av: {e}")
        market = None
        if self.cfg.get("apify_token") and any(w.get("marketplace_url") for w in self.cfg["watches"]):
            try:
                from bot import marketplace as mp
                market = mp.MarketplaceFinder(self.cfg, mp.ApifyClient(self.cfg["apify_token"]), finder, self.log)
                self.log("Facebook Marketplace: på.")
            except Exception as e:  # noqa: BLE001
                self.log(f"Facebook Marketplace av: {e}")
        poll = max(3, int(self.cfg.get("poll_minutes", 10)))
        while not self.stop_event.is_set():
            self.checking = True
            try:
                if watcher:
                    try:
                        watcher.cycle()
                    except Exception as e:  # noqa: BLE001
                        self.log(f"Tradera-fel: {e}")
                finder.cycle()
                if market:
                    try:
                        market.cycle()
                    except Exception as e:  # noqa: BLE001
                        self.log(f"Marketplace-fel: {e}")
            except Exception as e:  # noqa: BLE001
                self.log(f"Fel: {e}")
            self.checking = False
            self.wake.clear()
            self.next_check = time.time() + poll * 60
            for _ in range(poll * 60):
                if self.stop_event.is_set():
                    return
                if self.wake.wait(timeout=1):
                    break


# ---------------- deals and log ----------------

def read_deals(limit: int = 300) -> list[dict]:
    rows: list[dict] = []
    for name, kind in (("alerts.csv", "listing"), ("auction_alerts.csv", "auction")):
        p = JOURNAL_DIR / name
        if not p.exists() or p.stat().st_size == 0:
            continue
        try:
            with open(p, encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    ms = int(float(r.get("ms") or 0))
                    if kind == "listing":
                        source = "Facebook Marketplace" if "marketplace" in str(r.get("kind", "")) else "Blocket"
                        rows.append({"ms": ms, "time": r.get("time", ""), "watch": r.get("watch", ""), "title": r.get("heading", ""),
                                     "price": float(r.get("price") or 0), "reference": float(r.get("reference") or 0),
                                     "under_pct": float(r.get("discount_pct") or 0), "margin": float(r.get("expected_margin") or 0),
                                     "location": r.get("location", ""), "shipping": str(r.get("shipping", "0")) == "1",
                                     "url": r.get("url", ""), "source": source, "drop": str(r.get("kind", "")).startswith("drop")})
                    else:
                        rows.append({"ms": ms, "time": r.get("time", ""), "watch": r.get("watch", ""), "title": r.get("title", ""),
                                     "price": float(r.get("next_bid") or 0), "reference": float(r.get("reference") or 0),
                                     "under_pct": float(r.get("discount_pct") or 0), "margin": 0.0, "location": "",
                                     "shipping": False, "url": r.get("url", ""), "source": f"Tradera, {r.get('minutes_left', '?')} min kvar",
                                     "drop": False})
        except Exception:  # noqa: BLE001
            continue
    rows.sort(key=lambda x: -x["ms"])
    return rows[:limit]


class LogBuffer:
    def __init__(self, n: int = 300):
        self.lines: deque = deque(maxlen=n)
        self.lock = threading.Lock()

    def __call__(self, msg: str) -> None:
        with self.lock:
            self.lines.append(time.strftime("%H:%M:%S ") + str(msg))

    def tail(self, n: int = 200) -> list[str]:
        with self.lock:
            return list(self.lines)[-n:]
