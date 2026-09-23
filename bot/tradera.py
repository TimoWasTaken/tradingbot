"""Tradera integration for the deal finder: real sold prices and auctions that are about to end too cheaply.

Two things Blocket cannot give you:
  1. A register of what items ACTUALLY sell for. Tradera's search can return ended auctions with their winning bid,
     so the bot builds a sold-price register per watch on the first run and tops it up every day. The median of
     those real sales becomes the reference for the Blocket alerts instead of "what other sellers are asking".
  2. Auctions ending within the next hours where the next bid is far below that value.

Needs a free developer key from https://api.tradera.com/register (App ID + App Key in secrets.json).
Rate limit is 10,000 calls per day per method; this uses a few hundred."""
from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
import time
from pathlib import Path

import requests

from .blocket import passes
from .config import fmt_ms, fmt_num, now_ms

BASE = "https://api.tradera.com/v4"
SOLD_FIELDS = ["recorded", "watch", "item_id", "title", "item_type", "final_price", "bids", "end_date", "url"]
AUCTION_ALERT_FIELDS = ["id", "time", "ms", "watch", "item_id", "title", "current_bid", "next_bid", "reference", "reference_kind",
                        "discount_pct", "bids", "ends", "minutes_left", "url"]


class TraderaAPI:
    def __init__(self, app_id: str, app_key: str):
        if not str(app_id).strip() or not str(app_key).strip() or "PASTE" in str(app_key).upper():
            raise ValueError("No Tradera key. Register at https://api.tradera.com/register, create an application and put "
                             "tradera_app_id and tradera_app_key in secrets.json.")
        self.h = {"X-App-Id": str(app_id).strip(), "X-App-Key": str(app_key).strip(), "Accept": "application/json",
                  "Content-Type": "application/json"}
        self.calls = 0

    def _req(self, method: str, path: str, tries: int = 3, **kw):
        last: Exception | None = None
        for attempt in range(tries):
            try:
                self.calls += 1
                r = requests.request(method, BASE + path, headers=self.h, timeout=30, **kw)
                if r.status_code == 429:
                    time.sleep(30)
                    continue
                if r.status_code == 401:
                    raise RuntimeError("Tradera rejected the App ID / App Key (401).")
                r.raise_for_status()
                return r.json()
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"Tradera request failed: {path}: {last}")

    def search(self, words: str, page: int = 1, order_by: str = "EndDateAscending", ended: bool = False,
               per_page: int = 100, item_type: str | None = None) -> tuple[list[dict], int]:
        """Advanced search. ended=True returns finished listings with their final bid. Returns (items, total pages)."""
        body: dict = {"searchWords": words, "orderBy": order_by, "pageNumber": page, "itemsPerPage": per_page}
        if ended:
            body["itemStatus"] = "Ended"
        if item_type:
            body["itemType"] = item_type
        j = self._req("POST", "/search/advanced", json=body)
        items = [x for x in (parse_item(i) for i in j.get("items", [])) if x]
        return items, int(j.get("totalNumberOfPages") or 0)


def _parse_date(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone(dt.timedelta(hours=2)))   # Tradera times are Swedish local time
    return d


def parse_item(i: dict) -> dict | None:
    title = str(i.get("shortDescription") or "").strip()
    if not title or not i.get("id"):
        return None
    return {"id": int(i["id"]), "title": title, "max_bid": float(i.get("maxBid") or 0), "next_bid": float(i.get("nextBid") or 0),
            "buy_now": float(i.get("buyItNowPrice") or 0), "bids": int(i.get("bidCount") or 0), "has_bids": bool(i.get("hasBids")),
            "ended": bool(i.get("isEnded")), "item_type": str(i.get("itemType") or ""), "end": _parse_date(i.get("endDate")),
            "url": str(i.get("itemUrl") or ""), "seller": str(i.get("sellerAlias") or ""), "dsr": float(i.get("sellerDsrAverage") or 0)}


class SoldRegister:
    """sold.csv: every ended listing seen per watch, with the final price (0 = ended unsold)."""

    def __init__(self, jdir: Path):
        self.path = Path(jdir) / "sold.csv"
        self._cache: dict[str, list[dict]] | None = None

    def rows(self) -> list[dict]:
        if self._cache is None:
            self._cache = {}
            if self.path.exists() and self.path.stat().st_size > 0:
                with open(self.path, encoding="utf-8", newline="") as f:
                    for r in csv.DictReader(f):
                        self._cache.setdefault(r["watch"], []).append(r)
        return self._cache

    def ids(self, watch: str) -> set[str]:
        return {r["item_id"] for r in self.rows().get(watch, [])}

    def prices(self, watch: str) -> list[float]:
        return [float(r["final_price"]) for r in self.rows().get(watch, []) if float(r["final_price"] or 0) > 0]

    def sell_through(self, watch: str) -> float | None:
        rs = self.rows().get(watch, [])
        return (len([r for r in rs if float(r["final_price"] or 0) > 0]) / len(rs)) if rs else None

    def median(self, watch: str, min_samples: int = 8, window: int = 100) -> tuple[float | None, int]:
        p = self.prices(watch)
        if len(p) < min_samples:
            return None, len(p)
        return float(statistics.median(p[-window:])), len(p)

    def add(self, rows: list[dict]) -> None:
        if not rows:
            return
        new = not self.path.exists() or self.path.stat().st_size == 0
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SOLD_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow(r)
        self._cache = None


TEXT = {
    "en": {"sold": "sold on Tradera", "asked": "asked on Blocket",
           "line": "Ends in {m:.0f} min, {b} bids, next bid {nb} kr.",
           "body": ("'{w}' usually goes for {ref} kr ({kind}, {n} samples), so this is {u:.0f}% under. Others may snipe in the last "
                    "seconds; decide your max and stick to it."),
           "title": "Auction #{id}: {w} {u:.0f}% under, {m:.0f} min left"},
    "sv": {"sold": "sålt på Tradera", "asked": "utropspris på Blocket",
           "line": "Slutar om {m:.0f} min, {b} bud, nästa bud {nb} kr.",
           "body": ("'{w}' går vanligen för {ref} kr ({kind}, {n} exempel), så detta är {u:.0f}% under. Andra kan lägga bud i sista "
                    "sekunden; bestäm ditt max och håll dig till det."),
           "title": "Auktion #{id}: {w} {u:.0f}% under, {m:.0f} min kvar"},
}


class TraderaWatcher:
    def __init__(self, cfg: dict, api: TraderaAPI, jdir: Path, notifier, log, blocket_reference=None):
        self.cfg = cfg
        self.tx = TEXT.get(str(cfg.get("language", "en")).lower()[:2], TEXT["en"])
        self.tcfg = cfg.get("tradera", {})
        self.api = api
        self.jdir = Path(jdir)
        self.jdir.mkdir(parents=True, exist_ok=True)
        self.notifier = notifier
        self.log = log
        self.blocket_reference = blocket_reference      # callable(watch) -> (asking-price reference, n) as a fallback
        self.sold = SoldRegister(self.jdir)
        self.state_path = self.jdir / "tradera_state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else \
            {"sold_refreshed": {}, "alerted": {}, "alert_counter": 0, "last_cycle_ms": 0}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def _append(self, name: str, fields: list[str], row: dict) -> None:
        p = self.jdir / name
        new = not p.exists() or p.stat().st_size == 0
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    # ---------- reference ----------
    def reference(self, watch: dict) -> tuple[float | None, str, int]:
        """(reference, kind, samples): real sold prices when we have enough, else Blocket asking prices."""
        med, n = self.sold.median(watch["name"], int(self.tcfg.get("min_sold_samples", 8)))
        if med:
            return med, self.tx["sold"], n
        if self.blocket_reference:
            ref, n2 = self.blocket_reference(watch)
            if ref:
                return ref, self.tx["asked"], n2
        return None, "", 0

    # ---------- sold-price register ----------
    def refresh_sold(self, watch: dict, force: bool = False) -> int:
        """Fetches ended listings for the watch. First time: several pages of history; afterwards once a day, one page."""
        name = watch["name"]
        last = int(self.state["sold_refreshed"].get(name, 0))
        first = last == 0
        if not first and not force and now_ms() - last < 20 * 3_600_000:
            return 0
        pages = int(self.tcfg.get("history_pages", 3)) if first else 1
        known = self.sold.ids(name)
        rows = []
        for page in range(1, pages + 1):
            items, total_pages = self.api.search(watch["q"], page=page, order_by="EndDateDescending", ended=True)
            for it in items:
                if str(it["id"]) in known:
                    continue
                listing = {"heading": it["title"], "price": max(it["max_bid"], it["buy_now"], 1.0), "private": True}
                ok, _why = passes(listing, {**watch, "min_price": watch.get("sold_min_price", watch.get("min_price", 0)), "max_price": None})
                if not ok:
                    continue
                sold = it["bids"] > 0 or (it["item_type"] == "PureBuyItNow" and it["has_bids"])
                price = it["max_bid"] if it["bids"] > 0 else (it["buy_now"] if sold else 0.0)
                rows.append({"recorded": fmt_ms(now_ms()), "watch": name, "item_id": it["id"], "title": it["title"],
                             "item_type": it["item_type"], "final_price": round(price), "bids": it["bids"],
                             "end_date": it["end"].strftime("%Y-%m-%d") if it["end"] else "", "url": it["url"]})
                known.add(str(it["id"]))
            if page >= total_pages:
                break
            time.sleep(0.3)
        self.sold.add(rows)
        self.state["sold_refreshed"][name] = now_ms()
        return len(rows)

    # ---------- ending-soon alerts ----------
    def check_ending(self, watch: dict, items: list[dict]) -> int:
        hours = float(self.tcfg.get("ending_hours", 2))
        disc = float(watch.get("auction_discount_pct", self.tcfg.get("auction_discount_pct", 40))) / 100.0
        ref, kind, n = self.reference(watch)
        if not ref:
            return 0
        now = dt.datetime.now(dt.timezone.utc)
        alerts = 0
        for it in items:
            if it["ended"] or it["end"] is None or not it["item_type"].startswith("Auction") or str(it["id"]) in self.state["alerted"]:
                continue
            left = (it["end"] - now).total_seconds() / 60
            if left <= 0 or left > hours * 60:
                continue
            price_now = max(it["next_bid"], it["max_bid"], 1.0)      # what it costs you to take it right now
            listing = {"heading": it["title"], "price": price_now, "private": True}
            ok, _why = passes(listing, watch)
            if not ok or 1 - price_now / ref < disc:
                continue
            self.state["alert_counter"] += 1
            self.state["alerted"][str(it["id"])] = now_ms()
            under = (1 - price_now / ref) * 100
            row = {"id": self.state["alert_counter"], "time": fmt_ms(now_ms()), "ms": now_ms(), "watch": watch["name"], "item_id": it["id"],
                   "title": it["title"], "current_bid": it["max_bid"], "next_bid": it["next_bid"], "reference": round(ref),
                   "reference_kind": kind, "discount_pct": round(under, 1), "bids": it["bids"],
                   "ends": it["end"].strftime("%Y-%m-%d %H:%M"), "minutes_left": round(left), "url": it["url"]}
            self._append("auction_alerts.csv", AUCTION_ALERT_FIELDS, row)
            tx = self.tx
            msg = (f"{it['title']}\n" + tx["line"].format(m=left, b=it['bids'], nb=fmt_num(it['next_bid'], 0)) + "\n"
                   + tx["body"].format(w=watch['name'], ref=fmt_num(ref, 0), kind=kind, n=n, u=under) + f"\n{it['url']}")
            self.log(f"AUCTION #{row['id']} [{watch['name']}] {it['title']} next bid {it['next_bid']:.0f} vs ref {ref:.0f}, {left:.0f} min left {it['url']}")
            self.notifier.send(tx["title"].format(id=row['id'], w=watch['name'], u=under, m=left), msg, priority=4, tags=["hourglass"])
            alerts += 1
        cutoff = now_ms() - 7 * 86_400_000
        self.state["alerted"] = {k: v for k, v in self.state["alerted"].items() if int(v) > cutoff}
        return alerts

    # ---------- one cycle ----------
    def cycle(self) -> None:
        parts = []
        calls0 = self.api.calls
        for watch in self.cfg["watches"]:
            try:
                added = self.refresh_sold(watch)
                items, _ = self.api.search(watch["q"], page=1, order_by="EndDateAscending", item_type="Auction")
                al = self.check_ending(watch, items)
                med, n = self.sold.median(watch["name"], int(self.tcfg.get("min_sold_samples", 8)))
                st = self.sold.sell_through(watch["name"])
                parts.append(f"{watch['name']}: sold ref {(fmt_num(med, 0) + ' kr') if med else 'n/a'} ({n} sales"
                             f"{', ' + format(st * 100, '.0f') + '% sell-through' if st is not None else ''}), +{added} new, "
                             f"{len(items)} live auctions, {al} alerts")
                time.sleep(0.3)
            except Exception as e:  # noqa: BLE001
                parts.append(f"{watch['name']}: error {e}")
        self.log("Tradera | " + " | ".join(parts) + f" | {self.api.calls - calls0} API calls")
        self.state["last_cycle_ms"] = now_ms()
        self.save()
