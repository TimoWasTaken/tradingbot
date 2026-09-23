"""Blocket Deal Finder, desktop edition: one window, no Python knowledge needed.

Built into a single .exe with PyInstaller (see build_app.bat). Settings and the journal live in
%APPDATA%\\BlocketDealFinder so the .exe can sit anywhere. Reuses bot/blocket.py (Blocket), bot/tradera.py
(real sale prices, ending auctions) and bot/marketplace.py (Facebook Marketplace via Apify) for all the logic.
Text is available in Swedish and English (Settings tab). Runs as a 14-day trial without a licence key."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

# make "bot" and the app modules importable both from source and from the PyInstaller bundle
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import licensing as lic  # noqa: E402
from bot import blocket as bl  # noqa: E402
from bot.notify import Notifier  # noqa: E402

APP_NAME = "BlocketDealFinder"
VERSION = "1.1"
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = DATA_DIR / "config.json"
JOURNAL_DIR = DATA_DIR / "journal"
SALES_URL = "https://timowastaken.github.io/tradingbot/dealfinder/"
BUY_URL = SALES_URL + "#kop"

REGIONS = [("", "Hela Sverige / All of Sweden"), ("0.300001", "Stockholm"), ("0.300003", "Uppsala"), ("0.300004", "Södermanland"),
           ("0.300005", "Östergötland"), ("0.300006", "Jönköping"), ("0.300007", "Kronoberg"), ("0.300008", "Kalmar"),
           ("0.300009", "Gotland"), ("0.300010", "Blekinge"), ("0.300012", "Skåne"), ("0.300013", "Halland"),
           ("0.300014", "Västra Götaland"), ("0.300017", "Värmland"), ("0.300018", "Örebro"), ("0.300019", "Västmanland"),
           ("0.300020", "Dalarna"), ("0.300021", "Gävleborg"), ("0.300022", "Västernorrland"), ("0.300023", "Jämtland"),
           ("0.300024", "Västerbotten"), ("0.300025", "Norrbotten")]

COMMON_EXCLUDE = ["sökes", "söker", "köpes", "önskas", "defekt", "trasig", "trasigt", "reservdel", "reservdelar", "skadad",
                  "krossad", "bytes", "hyr", "uthyres", "hyres"]


def default_watches() -> list[dict]:
    def w(name, q, mn, mx, must=None, excl=(), pages=1, alert_below=None):
        d = {"name": name, "q": q, "min_price": mn, "max_price": mx, "exclude_words": COMMON_EXCLUDE + list(excl), "pages": pages}
        if must:
            d["must_words"] = must
        if alert_below:
            d["alert_below"] = alert_below
        return d
    return [
        w("Festool", "festool", 500, 15000, excl=["sågblad", "slippapper", "påsar", "tillbehör"], pages=2, alert_below=900),
        w("Hilti", "hilti", 500, 15000, excl=["spik", "bult", "plugg", "tillbehör"], pages=2, alert_below=900),
        w("Makita 18V", "makita 18v", 800, 6000, must=["makita"], excl=["batteri", "laddare", "bits", "sågblad", "väska", "låda"]),
        w("Bugaboo", "bugaboo", 1500, 9000, must=["fox", "donkey", "cameleon", "buffalo", "bee", "dragonfly", "giraffe", "vagn"],
          excl=["tillbehör", "regnskydd", "åkpåse", "fotsack", "adapter", "liggdel", "sittdel", "turtle", "resesäng", "ram", "chassi",
                "babynest", "ståbräda", "isofix", "bilbarnstol", "base", "klädsel", "sufflett", "korg", "hjul", "väska"], pages=2),
        w("Thule Chariot", "thule chariot", 1500, 12000, must=["chariot"],
          excl=["cykelhållare", "takbox", "takräcke", "adapter", "kit", "hjul", "regnskydd", "tillbehör"], pages=2),
        w("Elcykel", "elcykel", 3000, 25000, must=["elcykel", "el-cykel", "e-bike"], excl=["batteri", "laddare", "cykelhållare", "kit"], pages=2),
        w("Weber Genesis", "weber genesis", 1000, 12000, must=["weber"], excl=["överdrag", "galler", "tillbehör", "kol"]),
        w("Automower", "automower", 2000, 20000, must=["automower"], excl=["knivar", "batteri", "laddstation", "kabel", "tillbehör"], pages=2),
        w("Louis Poulsen", "louis poulsen", 800, 15000, must=["poulsen"], excl=["kopia", "replika", "skärm", "reservglas"], pages=2, alert_below=1200),
        w("String hylla", "string hylla", 500, 15000, must=["string"], excl=["kopia"], pages=2),
        w("Concept2", "concept2", 3000, 15000, must=["concept"], excl=["tillbehör"]),
        w("Canon RF objektiv", "canon rf", 2000, 30000, must=["rf"], excl=["kamerahus", "adapter", "väska", "filter"], pages=2),
    ]


def default_config() -> dict:
    return {"ntfy_topic": "", "poll_minutes": 10, "discount_alert_pct": 30, "location": "", "language": "sv",
            "autostart": False, "min_samples": 15, "price_window": 200, "resale_factor": 0.9,
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
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    if not cfg.get("trial_started"):
        cfg["trial_started"] = dt.date.today().isoformat()
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def license_status(cfg: dict) -> tuple[bool, str, str]:
    """(may run, status key, formatted argument)."""
    if lic.valid(cfg.get("license_email", ""), cfg.get("license_key", "")):
        return True, "lic_ok", cfg.get("license_email", "")
    days = lic.trial_days_left(cfg.get("trial_started", ""))
    if days > 0:
        return True, "lic_trial", str(days)
    return False, "lic_over", ""


# ---------------- texts ----------------

T = {
    "en": {
        "title": "Blocket Deal Finder", "tab_watches": "Watches", "tab_deals": "Deals", "tab_settings": "Settings", "tab_log": "Log",
        "start": "Start watching", "stop": "Stop", "check_now": "Check now", "status_idle": "Not running",
        "status_run": "Running: checking every {n} min",
        "name": "Name", "query": "Search text on Blocket", "min": "Min price", "max": "Max price", "must": "Must contain (one of, comma-separated)",
        "exclude": "Exclude words (comma-separated)", "alert_below": "Always alert below (kr, optional)", "discount": "Alert when % under going price",
        "pages": "Pages to fetch (1-3)", "mp_url": "Facebook Marketplace search URL (optional)",
        "add": "Add", "edit": "Edit", "remove": "Remove", "save": "Save", "cancel": "Cancel",
        "ntfy": "ntfy topic (your private channel name)", "ntfy_help": "Install the ntfy app on your phone, subscribe to a secret topic name, type the same name here.",
        "test": "Send test notification", "poll": "Check every (minutes)", "region": "Region", "autostart": "Start with Windows",
        "language": "Language", "save_settings": "Save settings", "open_data": "Open data folder", "help": "How it works", "guide": "Guide (web)",
        "help_text": ("Every few minutes the app fetches the newest listings for each watch, learns what things really sell for "
                      "(Tradera sale prices when you add a free key, otherwise Blocket asking prices scaled down), and sends a "
                      "push to your phone when a new listing is far enough under that (or drops to it). The first pass only "
                      "learns, so you are not flooded with old listings. Nothing is bought or messaged automatically. Check the "
                      "listing yourself, act fast, and for bikes always check the frame number against the police register."),
        "sent": "Test sent, check your phone.", "not_sent": "Could not send. Check the topic name and your internet connection.",
        "need_topic": "Enter your ntfy topic first (Settings tab).", "saved": "Saved.", "need_name": "Name and search text are required.",
        "confirm_remove": "Remove watch '{n}'?", "no_watches": "Add at least one watch first.",
        "tradera_id": "Tradera App ID (optional)", "tradera_key": "Tradera App Key (optional)",
        "tradera_help": ("With a free developer key from api.tradera.com/register the app also learns what things REALLY sell for "
                         "(ended Tradera auctions) and warns you about auctions ending within 2 hours far below that price."),
        "apify": "Apify token (optional, for Facebook Marketplace)",
        "apify_help": ("Facebook has no API. With a free account at apify.com (about 0.6 cents per listing, 5 USD free credit per month) "
                       "the app also checks Marketplace a few times a day for the watches that have a Marketplace search URL."),
        "license_group": "Licence", "license_email": "E-mail", "license_key": "Licence key", "activate": "Activate", "buy": "Buy a licence",
        "lic_ok": "Licence active for {a}", "lic_trial": "Trial: {a} days left", "lic_over": "The trial is over. Buy a licence to keep going.",
        "lic_bad": "The key does not match the e-mail address.", "lic_activated": "Licence activated. Thank you!",
        "deals_refresh": "Refresh", "deals_none": "No deals yet. They appear here as they are found (double-click opens the listing).",
        "col_time": "Time", "col_watch": "Watch", "col_heading": "Listing", "col_price": "Price", "col_ref": "Sells for", "col_under": "Under", "col_source": "Where",
    },
    "sv": {
        "title": "Blocket Deal Finder", "tab_watches": "Bevakningar", "tab_deals": "Fynd", "tab_settings": "Inställningar", "tab_log": "Logg",
        "start": "Starta bevakning", "stop": "Stoppa", "check_now": "Kolla nu", "status_idle": "Kör inte",
        "status_run": "Kör: kollar var {n}:e minut",
        "name": "Namn", "query": "Söktext på Blocket", "min": "Lägsta pris", "max": "Högsta pris", "must": "Måste innehålla (något av, kommaseparerat)",
        "exclude": "Uteslut ord (kommaseparerat)", "alert_below": "Larma alltid under (kr, valfritt)", "discount": "Larma vid % under gångpris",
        "pages": "Sidor att hämta (1-3)", "mp_url": "Sök-URL på Facebook Marketplace (valfritt)",
        "add": "Lägg till", "edit": "Ändra", "remove": "Ta bort", "save": "Spara", "cancel": "Avbryt",
        "ntfy": "ntfy-ämne (ditt privata kanalnamn)", "ntfy_help": "Installera appen ntfy på telefonen, prenumerera på ett hemligt ämnesnamn och skriv samma namn här.",
        "test": "Skicka testnotis", "poll": "Kolla var (minuter)", "region": "Region", "autostart": "Starta med Windows",
        "language": "Språk", "save_settings": "Spara inställningar", "open_data": "Öppna datamappen", "help": "Så fungerar det", "guide": "Guide (webb)",
        "help_text": ("Med några minuters mellanrum hämtar programmet de nyaste annonserna för varje bevakning, lär sig vad saker "
                      "faktiskt säljs för (Traderas försäljningspriser om du lägger in en gratis nyckel, annars Blockets utropspriser "
                      "nedskalade), och skickar en push till telefonen när en ny annons ligger tillräckligt långt under det (eller "
                      "sänks dit). Första passet lär bara, så du dränks inte i gamla annonser. Inget köps eller skrivs automatiskt. "
                      "Kolla annonsen själv, var snabb, och kontrollera alltid ramnummer mot polisens register innan du betalar för en cykel."),
        "sent": "Testnotis skickad, kolla telefonen.", "not_sent": "Kunde inte skicka. Kontrollera ämnesnamnet och internet.",
        "need_topic": "Fyll i ditt ntfy-ämne först (fliken Inställningar).", "saved": "Sparat.", "need_name": "Namn och söktext krävs.",
        "confirm_remove": "Ta bort bevakningen '{n}'?", "no_watches": "Lägg till minst en bevakning först.",
        "tradera_id": "Tradera App ID (valfritt)", "tradera_key": "Tradera App Key (valfritt)",
        "tradera_help": ("Med en gratis utvecklarnyckel från api.tradera.com/register lär sig programmet också vad saker FAKTISKT säljs för "
                         "(avslutade Tradera-auktioner) och varnar för auktioner som slutar inom 2 timmar långt under det priset."),
        "apify": "Apify-token (valfritt, för Facebook Marketplace)",
        "apify_help": ("Facebook har inget API. Med ett gratiskonto på apify.com (ca 6 öre per annons, 5 dollar gratis per månad) kollar "
                       "programmet också Marketplace några gånger om dagen för de bevakningar som har en Marketplace-sök-URL."),
        "license_group": "Licens", "license_email": "E-post", "license_key": "Licensnyckel", "activate": "Aktivera", "buy": "Köp licens",
        "lic_ok": "Licens aktiv för {a}", "lic_trial": "Provperiod: {a} dagar kvar", "lic_over": "Provperioden är slut. Köp en licens för att fortsätta.",
        "lic_bad": "Nyckeln matchar inte e-postadressen.", "lic_activated": "Licensen är aktiverad. Tack!",
        "deals_refresh": "Uppdatera", "deals_none": "Inga fynd än. De dyker upp här när de hittas (dubbelklick öppnar annonsen).",
        "col_time": "Tid", "col_watch": "Bevakning", "col_heading": "Annons", "col_price": "Pris", "col_ref": "Säljs för", "col_under": "Under", "col_source": "Var",
    },
}


# ---------------- worker ----------------

class Worker(threading.Thread):
    def __init__(self, cfg: dict, log_q: queue.Queue):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.log_q = log_q
        self.stop_event = threading.Event()
        self.wake = threading.Event()

    def log(self, msg: str) -> None:
        self.log_q.put(time.strftime("%H:%M:%S ") + msg)

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
                self.log("Tradera: on (real sale prices and ending auctions).")
            except Exception as e:  # noqa: BLE001
                self.log(f"Tradera off: {e}")
        market = None
        if self.cfg.get("apify_token") and any(w.get("marketplace_url") for w in self.cfg["watches"]):
            try:
                from bot import marketplace as mp
                market = mp.MarketplaceFinder(self.cfg, mp.ApifyClient(self.cfg["apify_token"]), finder, self.log)
                self.log("Facebook Marketplace: on.")
            except Exception as e:  # noqa: BLE001
                self.log(f"Facebook Marketplace off: {e}")
        poll = max(3, int(self.cfg.get("poll_minutes", 10)))
        while not self.stop_event.is_set():
            try:
                if watcher:
                    try:
                        watcher.cycle()
                    except Exception as e:  # noqa: BLE001
                        self.log(f"Tradera error: {e}")
                finder.cycle()
                if market:
                    try:
                        market.cycle()
                    except Exception as e:  # noqa: BLE001
                        self.log(f"Marketplace error: {e}")
            except Exception as e:  # noqa: BLE001
                self.log(f"Error: {e}")
            self.wake.clear()
            for _ in range(poll * 60):
                if self.stop_event.is_set():
                    return
                if self.wake.wait(timeout=1):
                    break


# ---------------- GUI ----------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.lang = self.cfg.get("language", "sv")
        self.worker: Worker | None = None
        self.log_q: queue.Queue = queue.Queue()
        self.deal_urls: dict[str, str] = {}
        self.title(f"{self.t('title')} {VERSION}")
        self.geometry("900x620")
        self.minsize(760, 500)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build()
        self.after(500, self.drain_log)
        self.after(30_000, self.auto_refresh_deals)
        if not self.cfg.get("ntfy_topic"):
            self.after(300, lambda: self.nb.select(self.tab_settings))

    def t(self, key: str, **kw) -> str:
        return T.get(self.lang, T["en"]).get(key, key).format(**kw)

    # ---------- layout ----------
    def build(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.btn_start = ttk.Button(top, text=self.t("start"), command=self.toggle)
        self.btn_start.pack(side="left")
        self.btn_now = ttk.Button(top, text=self.t("check_now"), command=self.check_now)
        self.btn_now.pack(side="left", padx=6)
        self.status = ttk.Label(top, text=self.t("status_idle"))
        self.status.pack(side="left", padx=12)
        ttk.Button(top, text=self.t("help"), command=lambda: messagebox.showinfo(self.t("help"), self.t("help_text"))).pack(side="right")
        ttk.Button(top, text=self.t("guide"), command=lambda: webbrowser.open(SALES_URL)).pack(side="right", padx=6)
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tab_watches = ttk.Frame(self.nb, padding=8)
        self.tab_deals = ttk.Frame(self.nb, padding=8)
        self.tab_settings = ttk.Frame(self.nb, padding=8)
        self.tab_log = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_watches, text=self.t("tab_watches"))
        self.nb.add(self.tab_deals, text=self.t("tab_deals"))
        self.nb.add(self.tab_settings, text=self.t("tab_settings"))
        self.nb.add(self.tab_log, text=self.t("tab_log"))
        self.build_watches()
        self.build_deals()
        self.build_settings()
        self.build_log()
        if self.worker and self.worker.is_alive():
            self.btn_start.config(text=self.t("stop"))
            self.status.config(text=self.t("status_run", n=self.cfg.get("poll_minutes", 10)))

    def build_watches(self) -> None:
        f = self.tab_watches
        cols = ("name", "query", "min", "max", "discount", "alert_below")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=14)
        for c, wdt in zip(cols, (140, 200, 80, 80, 120, 120)):
            self.tree.heading(c, text=self.t(c))
            self.tree.column(c, width=wdt, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.refresh_tree()
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text=self.t("add"), command=lambda: self.edit_watch(None)).pack(side="left")
        ttk.Button(bar, text=self.t("edit"), command=self.edit_selected).pack(side="left", padx=6)
        ttk.Button(bar, text=self.t("remove"), command=self.remove_selected).pack(side="left")
        self.tree.bind("<Double-1>", lambda _e: self.edit_selected())

    def build_deals(self) -> None:
        f = self.tab_deals
        cols = ("time", "watch", "heading", "price", "ref", "under", "source")
        self.deals = ttk.Treeview(f, columns=cols, show="headings", height=14)
        for c, wdt in zip(cols, (110, 110, 330, 70, 80, 60, 90)):
            self.deals.heading(c, text=self.t(f"col_{c}"))
            self.deals.column(c, width=wdt, anchor="w")
        self.deals.pack(fill="both", expand=True)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text=self.t("deals_refresh"), command=self.refresh_deals).pack(side="left")
        self.deals_hint = ttk.Label(bar, text="", foreground="#555")
        self.deals_hint.pack(side="left", padx=12)
        self.deals.bind("<Double-1>", self.open_deal)
        self.refresh_deals()

    def refresh_deals(self) -> None:
        rows: list[tuple[int, tuple, str]] = []
        for name, kind in (("alerts.csv", "listing"), ("auction_alerts.csv", "auction")):
            p = JOURNAL_DIR / name
            if not p.exists() or p.stat().st_size == 0:
                continue
            try:
                with open(p, encoding="utf-8", newline="") as fh:
                    for r in csv.DictReader(fh):
                        ms = int(float(r.get("ms") or 0))
                        if kind == "listing":
                            src = "Blocket" if "(" not in str(r.get("kind", "")) else "Marketplace"
                            vals = (str(r.get("time", ""))[5:16], r.get("watch", ""), r.get("heading", ""), f"{float(r.get('price') or 0):,.0f}",
                                    f"{float(r.get('reference') or 0):,.0f}", f"{float(r.get('discount_pct') or 0):.0f}%", src)
                        else:
                            vals = (str(r.get("time", ""))[5:16], r.get("watch", ""), r.get("title", ""), f"{float(r.get('next_bid') or 0):,.0f}",
                                    f"{float(r.get('reference') or 0):,.0f}", f"{float(r.get('discount_pct') or 0):.0f}%",
                                    f"Tradera {r.get('minutes_left', '')} min")
                        rows.append((ms, vals, str(r.get("url", ""))))
            except Exception:  # noqa: BLE001
                continue
        rows.sort(key=lambda x: -x[0])
        self.deals.delete(*self.deals.get_children())
        self.deal_urls = {}
        for i, (_ms, vals, url) in enumerate(rows[:300]):
            iid = str(i)
            self.deals.insert("", "end", iid=iid, values=vals)
            self.deal_urls[iid] = url
        self.deals_hint.config(text=self.t("deals_none") if not rows else f"{len(rows)}")

    def auto_refresh_deals(self) -> None:
        if self.worker and self.worker.is_alive():
            try:
                self.refresh_deals()
            except Exception:  # noqa: BLE001
                pass
        self.after(30_000, self.auto_refresh_deals)

    def open_deal(self, _event=None) -> None:
        sel = self.deals.selection()
        if sel and self.deal_urls.get(sel[0]):
            webbrowser.open(self.deal_urls[sel[0]])

    def refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, w in enumerate(self.cfg["watches"]):
            self.tree.insert("", "end", iid=str(i), values=(w["name"], w["q"], w.get("min_price", 0), w.get("max_price", ""),
                                                            w.get("discount_alert_pct", self.cfg.get("discount_alert_pct", 30)),
                                                            w.get("alert_below", "")))

    def selected_index(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def edit_selected(self) -> None:
        i = self.selected_index()
        if i is not None:
            self.edit_watch(i)

    def remove_selected(self) -> None:
        i = self.selected_index()
        if i is None:
            return
        if messagebox.askyesno(self.t("remove"), self.t("confirm_remove", n=self.cfg["watches"][i]["name"])):
            del self.cfg["watches"][i]
            save_config(self.cfg)
            self.refresh_tree()

    def edit_watch(self, index: int | None) -> None:
        w = dict(self.cfg["watches"][index]) if index is not None else {"exclude_words": list(COMMON_EXCLUDE)}
        win = tk.Toplevel(self)
        win.title(self.t("edit") if index is not None else self.t("add"))
        win.grab_set()
        fields = [("name", "name", w.get("name", "")), ("query", "q", w.get("q", "")), ("min", "min_price", w.get("min_price", 0)),
                  ("max", "max_price", w.get("max_price", "")), ("must", "must_words", ", ".join(w.get("must_words", []))),
                  ("exclude", "exclude_words", ", ".join(w.get("exclude_words", []))), ("alert_below", "alert_below", w.get("alert_below", "")),
                  ("discount", "discount_alert_pct", w.get("discount_alert_pct", self.cfg.get("discount_alert_pct", 30))),
                  ("pages", "pages", w.get("pages", 1)), ("mp_url", "marketplace_url", w.get("marketplace_url", ""))]
        vars_: dict[str, tk.StringVar] = {}
        for r, (label, key, val) in enumerate(fields):
            ttk.Label(win, text=self.t(label)).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            v = tk.StringVar(value=str(val))
            ttk.Entry(win, textvariable=v, width=64).grid(row=r, column=1, padx=8, pady=4)
            vars_[key] = v

        def save() -> None:
            name, q = vars_["name"].get().strip(), vars_["q"].get().strip()
            if not name or not q:
                messagebox.showwarning(self.t("save"), self.t("need_name"))
                return
            new = {"name": name, "q": q}
            for key in ("min_price", "max_price", "alert_below", "discount_alert_pct", "pages"):
                raw = vars_[key].get().strip().replace(" ", "")
                if raw:
                    try:
                        new[key] = float(raw) if key != "pages" else max(1, min(3, int(float(raw))))
                    except ValueError:
                        pass
            new["min_price"] = new.get("min_price", 0)
            for key in ("must_words", "exclude_words"):
                words = [x.strip() for x in vars_[key].get().split(",") if x.strip()]
                if words:
                    new[key] = words
            url = vars_["marketplace_url"].get().strip()
            if url.startswith("http"):
                new["marketplace_url"] = url
            if index is None:
                self.cfg["watches"].append(new)
            else:
                self.cfg["watches"][index] = new
            save_config(self.cfg)
            self.refresh_tree()
            win.destroy()

        bar = ttk.Frame(win)
        bar.grid(row=len(fields), column=0, columnspan=2, pady=8)
        ttk.Button(bar, text=self.t("save"), command=save).pack(side="left", padx=6)
        ttk.Button(bar, text=self.t("cancel"), command=win.destroy).pack(side="left")

    def build_settings(self) -> None:
        f = self.tab_settings
        self.v_topic = tk.StringVar(value=self.cfg.get("ntfy_topic", ""))
        self.v_poll = tk.StringVar(value=str(self.cfg.get("poll_minutes", 10)))
        self.v_disc = tk.StringVar(value=str(self.cfg.get("discount_alert_pct", 30)))
        self.v_region = tk.StringVar(value=next((n for c, n in REGIONS if c == self.cfg.get("location", "")), REGIONS[0][1]))
        self.v_auto = tk.BooleanVar(value=bool(self.cfg.get("autostart", False)))
        self.v_lang = tk.StringVar(value="Svenska" if self.lang == "sv" else "English")
        self.v_tid = tk.StringVar(value=str(self.cfg.get("tradera_app_id", "")))
        self.v_tkey = tk.StringVar(value=str(self.cfg.get("tradera_app_key", "")))
        self.v_apify = tk.StringVar(value=str(self.cfg.get("apify_token", "")))
        self.v_lemail = tk.StringVar(value=str(self.cfg.get("license_email", "")))
        self.v_lkey = tk.StringVar(value=str(self.cfg.get("license_key", "")))
        r = 0
        ttk.Label(f, text=self.t("ntfy")).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_topic, width=40).grid(row=r, column=1, sticky="w", pady=4)
        ttk.Button(f, text=self.t("test"), command=self.test_push).grid(row=r, column=2, padx=8)
        r += 1
        ttk.Label(f, text=self.t("ntfy_help"), wraplength=720, foreground="#555").grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Label(f, text=self.t("tradera_id")).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_tid, width=14).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(f, text=self.t("tradera_key")).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_tkey, width=40).grid(row=r, column=1, sticky="w")
        ttk.Button(f, text="api.tradera.com", command=lambda: webbrowser.open("https://api.tradera.com/register")).grid(row=r, column=2, padx=8)
        r += 1
        ttk.Label(f, text=self.t("tradera_help"), wraplength=720, foreground="#555").grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Label(f, text=self.t("apify")).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_apify, width=40).grid(row=r, column=1, sticky="w")
        ttk.Button(f, text="apify.com", command=lambda: webbrowser.open("https://apify.com")).grid(row=r, column=2, padx=8)
        r += 1
        ttk.Label(f, text=self.t("apify_help"), wraplength=720, foreground="#555").grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Label(f, text=self.t("poll")).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_poll, width=8).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(f, text=self.t("discount")).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v_disc, width=8).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(f, text=self.t("region")).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.v_region, values=[n for _c, n in REGIONS], state="readonly", width=30).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(f, text=self.t("autostart"), variable=self.v_auto).grid(row=r, column=0, columnspan=2, sticky="w", pady=4)
        r += 1
        ttk.Label(f, text=self.t("language")).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.v_lang, values=["Svenska", "English"], state="readonly", width=12).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Button(f, text=self.t("save_settings"), command=self.save_settings).grid(row=r, column=0, sticky="w", pady=10)
        ttk.Button(f, text=self.t("open_data"), command=lambda: os.startfile(DATA_DIR) if DATA_DIR.exists() else None).grid(row=r, column=1, sticky="w")
        r += 1
        box = ttk.LabelFrame(f, text=self.t("license_group"), padding=8)
        box.grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 0))
        ttk.Label(box, text=self.t("license_email")).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=self.v_lemail, width=34).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(box, text=self.t("license_key")).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=self.v_lkey, width=34).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Button(box, text=self.t("activate"), command=self.activate).grid(row=1, column=2, padx=6)
        ttk.Button(box, text=self.t("buy"), command=lambda: webbrowser.open(BUY_URL)).grid(row=0, column=2, padx=6)
        self.lic_label = ttk.Label(box, text="", foreground="#555")
        self.lic_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.update_license_label()

    def build_log(self) -> None:
        self.log_text = tk.Text(self.tab_log, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    # ---------- licence ----------
    def update_license_label(self) -> None:
        ok, key, arg = license_status(self.cfg)
        self.lic_label.config(text=self.t(key, a=arg), foreground="#2a7" if key == "lic_ok" else ("#555" if ok else "#c33"))

    def activate(self) -> None:
        email, key = self.v_lemail.get().strip(), self.v_lkey.get().strip()
        if not lic.valid(email, key):
            messagebox.showwarning(self.t("license_group"), self.t("lic_bad"))
            return
        self.cfg["license_email"], self.cfg["license_key"] = email, key.upper()
        save_config(self.cfg)
        self.update_license_label()
        messagebox.showinfo(self.t("license_group"), self.t("lic_activated"))

    # ---------- actions ----------
    def save_settings(self) -> None:
        self.cfg["ntfy_topic"] = self.v_topic.get().strip()
        self.cfg["tradera_app_id"] = self.v_tid.get().strip()
        self.cfg["tradera_app_key"] = self.v_tkey.get().strip()
        self.cfg["apify_token"] = self.v_apify.get().strip()
        try:
            self.cfg["poll_minutes"] = max(3, int(float(self.v_poll.get())))
            self.cfg["discount_alert_pct"] = float(self.v_disc.get())
        except ValueError:
            pass
        self.cfg["location"] = next((c for c, n in REGIONS if n == self.v_region.get()), "")
        self.cfg["autostart"] = bool(self.v_auto.get())
        self.cfg["language"] = "sv" if self.v_lang.get() == "Svenska" else "en"
        save_config(self.cfg)
        self.set_autostart(self.cfg["autostart"])
        if self.cfg["language"] != self.lang:
            self.lang = self.cfg["language"]
            self.build()
        messagebox.showinfo(self.t("save_settings"), self.t("saved"))

    def set_autostart(self, enabled: bool) -> None:
        try:
            startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            link = startup / f"{APP_NAME}.bat"
            exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
            if enabled and startup.exists():
                link.write_text(f'@echo off\r\nstart "" "{exe}"\r\n', encoding="utf-8")
            elif link.exists():
                link.unlink()
        except Exception:  # noqa: BLE001
            pass

    def test_push(self) -> None:
        topic = self.v_topic.get().strip()
        if not topic:
            messagebox.showwarning(self.t("test"), self.t("need_topic"))
            return
        ok = Notifier(topic=topic).send(self.t("title"), self.t("help_text")[:120] + " ...", tags=["tada"])
        messagebox.showinfo(self.t("test"), self.t("sent") if ok else self.t("not_sent"))

    def check_now(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.wake.set()
            self.nb.select(self.tab_log)
        else:
            self.toggle()

    def toggle(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.stop_event.set()
            self.worker = None
            self.btn_start.config(text=self.t("start"))
            self.status.config(text=self.t("status_idle"))
            return
        ok, key, arg = license_status(self.cfg)
        if not ok:
            messagebox.showwarning(self.t("license_group"), self.t(key, a=arg))
            self.nb.select(self.tab_settings)
            return
        if not self.cfg.get("ntfy_topic"):
            messagebox.showwarning(self.t("start"), self.t("need_topic"))
            self.nb.select(self.tab_settings)
            return
        if not self.cfg["watches"]:
            messagebox.showwarning(self.t("start"), self.t("no_watches"))
            return
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        self.worker = Worker(json.loads(json.dumps(self.cfg)), self.log_q)
        self.worker.start()
        self.btn_start.config(text=self.t("stop"))
        self.status.config(text=self.t("status_run", n=self.cfg.get("poll_minutes", 10)))
        self.nb.select(self.tab_log)

    def drain_log(self) -> None:
        try:
            while True:
                line = self.log_q.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.after(500, self.drain_log)

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
