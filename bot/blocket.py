"""Blocket deal finder: watches searches on blocket.se, learns the going asking price per search from the listings it
sees, and pushes a notification when a new listing is priced well below that level (or drops to it).

The reference is the median asking price of listings that passed the same filters over the last N seen. That is a
proxy for market value; the real resale value is lower (you sell into the same market, minus haggling), which the
resale_factor accounts for. You can also pin a reference_price per watch from what things actually sell for.

Uses the public JSON search that the Blocket website itself calls, unauthenticated, at a gentle rate. Read Blocket's
terms before running this often or for anything commercial. Nothing is bought or messaged automatically."""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import statistics
import time
from pathlib import Path

import requests

from .config import fmt_ms, fmt_num, now_ms

SEARCH_URL = "https://www.blocket.se/recommerce/forsale/search/api/search/SEARCH_ID_BAP_COMMON"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
           "Accept": "application/json"}
LISTING_FIELDS = ["seen", "seen_ms", "watch", "ad_id", "heading", "price", "location", "posted", "posted_ms",
                  "shipping", "buy_now", "url", "passed_filters", "reason"]
ALERT_FIELDS = ["id", "time", "ms", "watch", "ad_id", "heading", "price", "reference", "discount_pct",
                "expected_resale", "expected_margin", "location", "shipping", "posted", "url", "kind"]
FLIP_FIELDS = ["id", "time", "watch", "heading", "bought", "sold", "costs", "profit", "note"]


# ---------------- API ----------------

def search(q: str, page: int = 1, sort: str = "PUBLISHED_DESC", location: str | None = None, tries: int = 3) -> dict:
    params: dict = {"q": q, "page": page, "sort": sort}
    if location:
        params["location"] = location
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=30)
            if r.status_code in (429, 503):
                time.sleep(20 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Blocket search failed for '{q}': {last}")


def parse_doc(d: dict) -> dict | None:
    price = (d.get("price") or {}).get("amount")
    if price is None:
        return None
    flags = set(d.get("flags") or [])
    return {
        "ad_id": str(d.get("ad_id") or d.get("id")), "heading": str(d.get("heading", "")).strip(),
        "price": float(price), "location": str(d.get("location", "")),
        "posted_ms": int(d.get("timestamp") or 0), "shipping": "shipping_exists" in flags, "buy_now": "buy_now" in flags,
        "private": "private" in flags, "url": str(d.get("canonical_url", "")),
        "extras": {e.get("id"): e.get("values") for e in (d.get("extras") or []) if isinstance(e, dict)},
    }


def available_locations(q: str = "cykel") -> list[tuple[str, str]]:
    """(value, display name) for the location filter, read from a search response."""
    j = search(q)
    for f in j.get("filters", []):
        if f.get("name") == "location":
            return [(it.get("value"), it.get("display_name")) for it in f.get("filter_items", [])]
    return []


# ---------------- filters ----------------

def _has_word(text: str, words: list[str]) -> str | None:
    t = text.lower()
    for w in words:
        if re.search(r"(?<![\wåäö])" + re.escape(w.lower()) + r"(?![\wåäö])", t):
            return w
    return None


def passes(listing: dict, watch: dict) -> tuple[bool, str]:
    head = listing["heading"]
    if listing["price"] < float(watch.get("min_price", 0)):
        return False, "below min_price (accessory or part)"
    if watch.get("max_price") and listing["price"] > float(watch["max_price"]):
        return False, "above max_price"
    w = _has_word(head, watch.get("exclude_words", []))
    if w:
        return False, f"excluded word '{w}'"
    must = watch.get("must_words", [])
    if must and not _has_word(head, must):
        return False, "missing required word"
    if watch.get("private_only", True) and not listing["private"]:
        return False, "business seller"
    return True, ""


# ---------------- deal finder ----------------

class DealFinder:
    def __init__(self, cfg: dict, jdir: Path, notifier, log):
        self.cfg = cfg
        self.jdir = Path(jdir)
        self.jdir.mkdir(parents=True, exist_ok=True)
        self.notifier = notifier
        self.log = log
        self.state_path = self.jdir / "state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else \
            {"seen": {}, "prices": {}, "alert_counter": 0}
        self.min_samples = int(cfg.get("min_samples", 15))
        self.window = int(cfg.get("price_window", 200))
        self.resale_factor = float(cfg.get("resale_factor", 0.9))
        self.sold_register = None       # optional bot.tradera.SoldRegister: real sale prices beat asking prices
        self.min_sold_samples = int(cfg.get("tradera", {}).get("min_sold_samples", 5))
        # Asking prices on Blocket run well above what things sell for: across our watches the Tradera sale median
        # was 46-79% of the Blocket ask median (typically ~55%). When no sale data exists, asks are scaled down by this.
        self.ask_to_sold_factor = float(cfg.get("ask_to_sold_factor", 0.6))
        # What you keep when you sell: Tradera takes ~10% plus payment fees; local cash sales cost haggling instead.
        self.selling_cost_pct = float(cfg.get("selling_cost_pct", 12))
        self.last_reference_kind = ""

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

    def asking_reference(self, watch: dict) -> tuple[float | None, int]:
        """Median of recent asking prices on Blocket for this watch."""
        prices = self.state["prices"].get(watch["name"], [])
        if len(prices) < self.min_samples:
            return None, len(prices)
        return float(statistics.median(prices[-self.window:])), len(prices)

    def reference(self, watch: dict) -> tuple[float | None, int]:
        """(reference = what the item is likely worth when SOLD, sample count).
        Priority: a pinned reference_price; real Tradera sale prices; with only a few sales, the lower of those and
        the scaled asking prices; otherwise Blocket asking prices scaled down by ask_to_sold_factor."""
        if watch.get("reference_price"):
            self.last_reference_kind = "your own reference price"
            return float(watch["reference_price"]), 0
        ask, n_ask = self.asking_reference(watch)
        ask_adj = ask * self.ask_to_sold_factor if ask else None
        if self.sold_register is not None:
            med, n = self.sold_register.median(watch["name"], 1)
            st = self.sold_register.sell_through(watch["name"])
            st_txt = f", only {st * 100:.0f}% of listings actually sell" if st is not None and st < 0.5 else ""
            if med and n >= self.min_sold_samples:
                self.last_reference_kind = f"real sale prices on Tradera, {n} sales{st_txt}"
                return med, n
            if med and n >= 3:
                ref = min(med, ask_adj) if ask_adj else med
                self.last_reference_kind = f"only {n} Tradera sales, so the lower of those and adjusted Blocket asks"
                return ref, n
        if ask_adj:
            self.last_reference_kind = (f"asking prices on Blocket scaled to typical sale value "
                                        f"({self.ask_to_sold_factor * 100:.0f}% of the ask median)")
            return ask_adj, n_ask
        return None, n_ask

    @staticmethod
    def is_deal(listing: dict, watch: dict, ref: float | None, threshold: float) -> bool:
        """A deal is either far enough under the learned reference, or under a hard 'alert_below' price
        (useful for brand watches like 'Festool' where the listings are too varied for one reference)."""
        if watch.get("alert_below") and listing["price"] <= float(watch["alert_below"]):
            return True
        return bool(ref) and 1 - listing["price"] / ref >= threshold

    def alert(self, listing: dict, watch: dict, ref: float, kind: str) -> None:
        discount = 1 - listing["price"] / ref
        resale = ref * (1 - self.selling_cost_pct / 100.0)      # what you keep after selling costs
        margin = resale - listing["price"]
        self.state["alert_counter"] += 1
        row = {"id": self.state["alert_counter"], "time": fmt_ms(now_ms()), "ms": now_ms(), "watch": watch["name"],
               "ad_id": listing["ad_id"], "heading": listing["heading"], "price": listing["price"], "reference": round(ref),
               "discount_pct": round(discount * 100, 1), "expected_resale": round(resale), "expected_margin": round(margin),
               "location": listing["location"], "shipping": int(listing["shipping"]), "posted": fmt_ms(listing["posted_ms"]),
               "url": listing["url"], "kind": kind}
        self._append("alerts.csv", ALERT_FIELDS, row)
        age_min = max(0, (now_ms() - listing["posted_ms"]) / 60000)
        msg = (f"{listing['heading']}\n{fmt_num(listing['price'], 0)} kr in {listing['location']}"
               f"{', can be shipped' if listing['shipping'] else ''}{', buy now' if listing['buy_now'] else ''}, "
               f"posted {age_min:.0f} min ago.\n"
               f"'{watch['name']}' usually SELLS for about {fmt_num(ref, 0)} kr (basis: {self.last_reference_kind or 'reference'}), "
               f"so this is {discount * 100:.0f}% under. "
               f"After ~{self.selling_cost_pct:.0f}% selling costs you would keep ~{fmt_num(resale, 0)} kr = "
               f"~{fmt_num(margin, 0)} kr margin, before transport. Check the exact model: the reference mixes all "
               f"'{watch['name']}' listings that pass your filters.\n{listing['url']}")
        title = f"Deal #{row['id']}: {watch['name']} {discount * 100:.0f}% under" + (" (price drop)" if kind == "drop" else "")
        self.log(f"ALERT #{row['id']} [{watch['name']}] {listing['heading']} {listing['price']:.0f} kr vs ref {ref:.0f} "
                 f"({discount * 100:.0f}% under) {listing['url']}")
        self.notifier.send(title, msg, priority=4, tags=["shopping_cart"])

    def process_watch(self, watch: dict) -> dict:
        threshold = float(watch.get("discount_alert_pct", self.cfg.get("discount_alert_pct", 30))) / 100.0
        pages = int(watch.get("pages", self.cfg.get("pages", 1)))
        new_count = alerts = 0
        seen = self.state["seen"]
        prices = self.state["prices"].setdefault(watch["name"], [])
        for page in range(1, pages + 1):
            docs = search(watch["q"], page=page, location=watch.get("location") or self.cfg.get("location")).get("docs", [])
            for d in docs:
                lst = parse_doc(d)
                if lst is None:
                    continue
                key = f"{watch['name']}|{lst['ad_id']}"
                ok, why = passes(lst, watch)
                prev = seen.get(key)
                if prev is None:
                    new_count += 1
                    seen[key] = {"price": lst["price"], "ms": now_ms(), "ok": ok}
                    self._append("listings.csv", LISTING_FIELDS, {
                        "seen": fmt_ms(now_ms()), "seen_ms": now_ms(), "watch": watch["name"], "ad_id": lst["ad_id"],
                        "heading": lst["heading"], "price": lst["price"], "location": lst["location"],
                        "posted": fmt_ms(lst["posted_ms"]), "posted_ms": lst["posted_ms"], "shipping": int(lst["shipping"]),
                        "buy_now": int(lst["buy_now"]), "url": lst["url"], "passed_filters": int(ok), "reason": why})
                    if ok:
                        ref, n = self.reference(watch)
                        if self.is_deal(lst, watch, ref, threshold) and not getattr(self, "learning", False):
                            self.alert(lst, watch, ref or float(watch.get("alert_below")), "new")
                            alerts += 1
                        prices.append(lst["price"])          # learn AFTER judging, so a deal does not drag the reference
                        del prices[:-self.window]
                elif ok and lst["price"] < float(prev["price"]) * 0.999:
                    ref, n = self.reference(watch)
                    prev["price"] = lst["price"]
                    if self.is_deal(lst, watch, ref, threshold) and not prev.get("alerted_drop"):
                        prev["alerted_drop"] = True
                        self.alert(lst, watch, ref or float(watch.get("alert_below")), "drop")
                        alerts += 1
            time.sleep(float(self.cfg.get("request_pause_seconds", 2)))
        ref, n = self.reference(watch)
        return {"new": new_count, "alerts": alerts, "reference": ref, "samples": n}

    def cycle(self) -> None:
        # forget ads not seen for 60 days so the state does not grow forever
        cutoff = now_ms() - 60 * 86_400_000
        self.state["seen"] = {k: v for k, v in self.state["seen"].items() if int(v.get("ms", 0)) > cutoff}
        # the very first pass only learns price levels; alerting on it would flood the phone with old listings
        self.learning = not self.state["seen"]
        if self.learning:
            self.log("First pass: learning price levels from the current listings, no alerts this round.")
        parts = []
        for watch in self.cfg["watches"]:
            try:
                r = self.process_watch(watch)
                ref = f"{r['reference']:.0f} kr" if r["reference"] else f"learning ({r['samples']}/{self.min_samples})"
                parts.append(f"{watch['name']}: {r['new']} new, {r['alerts']} deals, ref {ref}")
            except Exception as e:  # noqa: BLE001
                parts.append(f"{watch['name']}: error {e}")
        self.log(" | ".join(parts))
        self.save()


# ---------------- journal helpers ----------------

def load_csv(jdir: Path, name: str, fields: list[str]):
    """pandas is imported here so the standalone desktop app does not have to bundle it."""
    import pandas as pd

    p = Path(jdir) / name
    if p.exists() and p.stat().st_size > 0:
        return pd.read_csv(p, encoding="utf-8")
    return pd.DataFrame(columns=fields)


def add_flip(jdir: Path, watch: str, heading: str, bought: float, sold: float, costs: float, note: str) -> dict:
    """Record a real flip you did by hand, so the report can show actual profit next to the alerts."""
    jdir = Path(jdir)
    p = jdir / "flips.csv"
    existing = 0
    if p.exists() and p.stat().st_size > 0:
        with open(p, encoding="utf-8", newline="") as f:
            existing = sum(1 for _ in csv.DictReader(f))
    row = {"id": existing + 1, "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "watch": watch, "heading": heading,
           "bought": bought, "sold": sold, "costs": costs, "profit": round(sold - bought - costs, 2), "note": note}
    new = existing == 0
    with open(jdir / "flips.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FLIP_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    return row
