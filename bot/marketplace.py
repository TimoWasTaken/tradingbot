"""Facebook Marketplace via Apify's official "facebook-marketplace-scraper" actor.

Facebook has no API and blocks logged-out search, so the deal finder does not talk to Facebook at all. Instead it asks
Apify (apify.com) to run their maintained scraper on a Marketplace search URL and return the newest listings. Your
Facebook account is never involved. Apify charges per listing returned (about half a cent each on the free plan,
with 5 USD of free credit per month), so this runs a few times a day for a handful of watches, not every ten minutes.

Setup: create a free account at apify.com, copy the API token (Settings > Integrations) into secrets.json as
"apify_token", and give each watch a Marketplace search URL copied from your browser with your area and radius set
and "Sort by: Date listed" chosen. The listings are then judged exactly like Blocket listings."""
from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path

import requests

from .config import fmt_ms, now_ms

ACTOR = "apify~facebook-marketplace-scraper"
API = "https://api.apify.com/v2"


class ApifyClient:
    def __init__(self, token: str):
        if not token or "PASTE" in token.upper():
            raise ValueError("No Apify token. Create a free account at https://apify.com, copy the API token from "
                             "Settings > Integrations and put it in secrets.json as \"apify_token\".")
        self.token = token.strip()
        self.last_cost_items = 0

    def marketplace(self, url: str, limit: int = 10, timeout_s: int = 180) -> list[dict]:
        """Runs the actor synchronously and returns the dataset items (each item = one listing)."""
        r = requests.post(f"{API}/acts/{ACTOR}/run-sync-get-dataset-items",
                          params={"token": self.token, "timeout": timeout_s, "memory": 1024},
                          json={"startUrls": [{"url": url}], "resultsLimit": int(limit), "includeListingDetails": False},
                          timeout=timeout_s + 30)
        if r.status_code == 401:
            raise RuntimeError("Apify rejected the token (401).")
        if r.status_code == 402:
            raise RuntimeError("Apify says the account has no credit left (402).")
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Apify returned {r.status_code}: {r.text[:200]}")
        items = r.json()
        self.last_cost_items = len(items) if isinstance(items, list) else 0
        return items if isinstance(items, list) else []


def _num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"[\d][\d\s.,]*", str(v))
    if not m:
        return 0.0
    s = m.group(0).replace(" ", "").replace(" ", "")
    if s.count(",") and s.count("."):
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".") if s.count(",") == 1 and len(s.split(",")[-1]) <= 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_item(i: dict) -> dict | None:
    """Apify item -> the deal finder's listing dict. The actor's field names have varied between versions,
    so several candidates are tried for each value."""
    title = i.get("marketplace_listing_title") or i.get("title") or i.get("name") or ""
    price = None
    for key in ("listing_price", "price", "listingPrice"):
        v = i.get(key)
        if isinstance(v, dict):
            price = _num(v.get("amount") or v.get("formatted_amount") or v.get("formattedAmount"))
        elif v not in (None, ""):
            price = _num(v)
        if price:
            break
    if not title or not price:
        return None
    lid = str(i.get("id") or i.get("listingId") or i.get("listing_id") or i.get("url") or title)
    url = i.get("listingUrl") or i.get("url") or (f"https://www.facebook.com/marketplace/item/{lid}" if lid.isdigit() else "")
    loc = i.get("location") or i.get("locationText") or ""
    if isinstance(loc, dict):
        loc = (loc.get("reverse_geocode") or {}).get("city") or loc.get("city") or loc.get("name") or ""
    ts = i.get("creation_time") or i.get("creationTime") or i.get("listedAt") or i.get("timestamp") or 0
    posted_ms = 0
    if isinstance(ts, (int, float)) and ts > 0:
        posted_ms = int(ts * 1000) if ts < 1e12 else int(ts)
    elif isinstance(ts, str) and ts:
        try:
            posted_ms = int(dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            posted_ms = 0
    delivery = str(i.get("delivery_types") or i.get("deliveryTypes") or "").lower()
    status = str(i.get("status") or i.get("listing_status") or "").lower()
    if status and status not in ("live", "available", "active", ""):
        return None
    return {"ad_id": lid, "heading": str(title).strip(), "price": float(price), "location": str(loc),
            "posted_ms": posted_ms, "shipping": "ship" in delivery, "buy_now": False, "private": True, "url": str(url),
            "extras": {}}


class MarketplaceFinder:
    """Runs the Apify actor for every watch that has a Marketplace URL, then hands the listings to the DealFinder."""

    def __init__(self, cfg: dict, client: ApifyClient, finder, log):
        self.cfg = cfg
        self.mcfg = cfg.get("marketplace", {})
        self.client = client
        self.finder = finder
        self.log = log
        self.last_run_ms = int(finder.state.get("marketplace_last_run_ms", 0))

    def due(self) -> bool:
        every = float(self.mcfg.get("hours_between_runs", 4))
        return now_ms() - self.last_run_ms >= every * 3_600_000

    def cycle(self, force: bool = False) -> dict:
        if not force and not self.due():
            return {}
        watches = [w for w in self.cfg["watches"] if w.get("marketplace_url")]
        if not watches:
            return {}
        limit = int(self.mcfg.get("results_per_watch", 10))
        first = self.last_run_ms == 0
        parts, items_total = [], 0
        for w in watches:
            try:
                raw = self.client.marketplace(w["marketplace_url"], limit=limit)
                listings = [x for x in (parse_item(i) for i in raw) if x]
                items_total += len(raw)
                r = self.finder.process_listings(w, listings, "marketplace", learning=first)
                parts.append(f"{w['name']}: {len(raw)} fetched, {r['new']} new, {r['alerts']} deals")
            except Exception as e:  # noqa: BLE001
                parts.append(f"{w['name']}: error {e}")
            time.sleep(1)
        self.last_run_ms = now_ms()
        self.finder.state["marketplace_last_run_ms"] = self.last_run_ms
        self.finder.save()
        self.log(f"Marketplace (Apify) | " + " | ".join(parts) + f" | {items_total} listings billed"
                 + (" | first run: learning only" if first else ""))
        return {"items": items_total}
