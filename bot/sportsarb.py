"""Sports arbitrage measurement ("sure bets"): how often do bookmakers disagree enough that betting every outcome
at the best available odds guarantees a profit, how big is the gap, and at which bookmakers?

Odds come from The Odds API (the-odds-api.com, free plan 500 credits per month; one credit per sport per scan).
Nothing is ever bet. The point is to know what is on the table before opening a single bookmaker account,
because bookmakers limit winning accounts within weeks."""
from __future__ import annotations

import csv
import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import requests

from .config import fmt_ms, fmt_num, now_ms

API = "https://api.the-odds-api.com/v4"

EVENT_FIELDS = ["scan_time", "scan_ms", "sport", "event", "commence", "n_books", "margin_se", "margin_all",
                "best_se", "best_all"]
ARB_FIELDS = ["scan_time", "scan_ms", "sport", "event", "commence", "set", "profit_pct", "legs", "stakes_for_capital"]
SCAN_FIELDS = ["scan_time", "scan_ms", "sports", "events", "arbs_se", "arbs_all", "credits_used", "credits_remaining"]


class OddsAPI:
    def __init__(self, key: str):
        if not key or "PASTE" in key.upper():
            raise ValueError("No Odds API key. Register (free) at https://the-odds-api.com and put the key in secrets.json "
                             "under \"odds_api_key\".")
        self.key = key
        self.remaining: int | None = None
        self.used: int | None = None

    def sports(self) -> list[dict]:
        r = requests.get(f"{API}/sports", params={"apiKey": self.key}, timeout=30)
        r.raise_for_status()
        return r.json()

    def odds(self, sport: str, bookmakers: list[str], markets: str = "h2h") -> list[dict]:
        params = {"apiKey": self.key, "markets": markets, "oddsFormat": "decimal", "dateFormat": "unix"}
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        else:
            params["regions"] = "eu"
        r = requests.get(f"{API}/sports/{sport}/odds", params=params, timeout=30)
        if r.status_code == 404:
            return []          # sport out of season
        if r.status_code == 401:
            raise RuntimeError("The Odds API rejected the key (401). Check odds_api_key in secrets.json.")
        if r.status_code == 429:
            raise RuntimeError("The Odds API quota is used up for this month (429).")
        r.raise_for_status()
        self.remaining = int(r.headers.get("x-requests-remaining", -1))
        self.used = int(r.headers.get("x-requests-used", -1))
        return r.json()


# ---------------- arbitrage math ----------------

def best_odds(event: dict, allowed: set[str] | None) -> dict[str, tuple[float, str]]:
    """outcome name -> (best decimal odds, bookmaker key) among the allowed bookmakers (None = all)."""
    best: dict[str, tuple[float, str]] = {}
    for b in event.get("bookmakers", []):
        if allowed is not None and b["key"] not in allowed:
            continue
        for mk in b.get("markets", []):
            if mk.get("key") != "h2h":
                continue
            for o in mk.get("outcomes", []):
                price = float(o.get("price") or 0)
                name = str(o.get("name"))
                if price > 1.0 and (name not in best or price > best[name][0]):
                    best[name] = (price, b["key"])
    return best


def margin(best: dict[str, tuple[float, str]]) -> float | None:
    """Sum of 1/odds over the outcomes. Below 1.0 = arbitrage; 1.05 = the books keep 5%."""
    if len(best) < 2:
        return None
    return sum(1.0 / p for p, _b in best.values())


def stakes(best: dict[str, tuple[float, str]], capital: float) -> dict[str, float]:
    """How to split `capital` so every outcome pays the same amount."""
    m = margin(best) or 1.0
    return {name: round(capital * (1.0 / p) / m, 2) for name, (p, _b) in best.items()}


# ---------------- scanner ----------------

class ArbScanner:
    def __init__(self, cfg: dict, api: OddsAPI, jdir: Path, notifier, log):
        self.cfg = cfg
        self.api = api
        self.jdir = Path(jdir)
        self.jdir.mkdir(parents=True, exist_ok=True)
        self.notifier = notifier
        self.log = log
        self.licensed = set(cfg.get("bookmakers_se", []))
        self.all_books = list(dict.fromkeys(list(cfg.get("bookmakers_se", [])) + list(cfg.get("bookmakers_extra", []))))
        self.names = dict(cfg.get("bookmaker_names", {}))
        self.capital = float(cfg.get("capital_sek", 10000))
        self.min_profit = float(cfg.get("min_profit_pct", 0.3)) / 100.0
        self.seen_path = self.jdir / "seen.json"
        self.seen: dict[str, dict] = json.loads(self.seen_path.read_text(encoding="utf-8")) if self.seen_path.exists() else {}

    def book(self, key: str) -> str:
        return self.names.get(key, key)

    def _append(self, name: str, fields: list[str], row: dict) -> None:
        p = self.jdir / name
        new = not p.exists() or p.stat().st_size == 0
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def scan(self, notify: bool = True) -> dict:
        t0 = now_ms()
        stamp = fmt_ms(t0)
        n_events = 0
        arbs_se: list[dict] = []
        arbs_all: list[dict] = []
        for sport in self.cfg["sports"]:
            try:
                events = self.api.odds(sport, self.all_books)
            except Exception as e:  # noqa: BLE001
                self.log(f"{sport}: {e}")
                continue
            for ev in events:
                commence = int(ev.get("commence_time") or 0)
                if commence and commence * 1000 < t0:
                    continue                                   # already started (live odds behave differently)
                name = f"{ev.get('home_team')} vs {ev.get('away_team')}"
                b_se = best_odds(ev, self.licensed)
                b_all = best_odds(ev, None)
                m_se, m_all = margin(b_se), margin(b_all)
                n_events += 1
                self._append("events.csv", EVENT_FIELDS, {
                    "scan_time": stamp, "scan_ms": t0, "sport": sport, "event": name,
                    "commence": dt.datetime.fromtimestamp(commence).strftime("%Y-%m-%d %H:%M") if commence else "",
                    "n_books": len(ev.get("bookmakers", [])),
                    "margin_se": round(m_se, 5) if m_se else "", "margin_all": round(m_all, 5) if m_all else "",
                    "best_se": " | ".join(f"{k} {p:.2f}@{self.book(b)}" for k, (p, b) in b_se.items()),
                    "best_all": " | ".join(f"{k} {p:.2f}@{self.book(b)}" for k, (p, b) in b_all.items()),
                })
                for label, best, m, bucket in (("se", b_se, m_se, arbs_se), ("all", b_all, m_all, arbs_all)):
                    if m is not None and 1.0 - m >= self.min_profit:
                        profit = 1.0 - m
                        arb = {"scan_time": stamp, "scan_ms": t0, "sport": sport, "event": name,
                               "commence": dt.datetime.fromtimestamp(commence).strftime("%Y-%m-%d %H:%M") if commence else "",
                               "set": label, "profit_pct": round(profit * 100, 3),
                               "legs": " | ".join(f"{k} {p:.2f} @ {self.book(b)}" for k, (p, b) in best.items()),
                               "stakes_for_capital": " | ".join(f"{k} {s:,.0f} SEK" for k, s in stakes(best, self.capital).items()),
                               "_key": f"{ev.get('id')}|{label}"}
                        bucket.append(arb)
                        self._append("arbs.csv", ARB_FIELDS, arb)
            time.sleep(0.5)
        self._append("scans.csv", SCAN_FIELDS, {
            "scan_time": stamp, "scan_ms": t0, "sports": len(self.cfg["sports"]), "events": n_events,
            "arbs_se": len(arbs_se), "arbs_all": len(arbs_all),
            "credits_used": self.api.used if self.api.used is not None else "",
            "credits_remaining": self.api.remaining if self.api.remaining is not None else ""})
        self.log(f"Scan: {n_events} upcoming events in {len(self.cfg['sports'])} sports. Arbitrages among Swedish-licensed "
                 f"bookmakers: {len(arbs_se)}, among all bookmakers incl. sharp/exchange: {len(arbs_all)}. "
                 f"API credits left this month: {self.api.remaining}.")
        # notify about new Swedish-licensed arbs (each event once per day)
        for arb in arbs_se:
            key = arb["_key"]
            first = key not in self.seen or (t0 - int(self.seen[key]["ms"])) > 86_400_000
            self.seen[key] = {"ms": t0, "profit": arb["profit_pct"]}
            if first and notify:
                msg = (f"{arb['event']} ({arb['sport']}, starts {arb['commence']}).\nLegs: {arb['legs']}.\n"
                       f"Stakes for {self.capital:,.0f} SEK: {arb['stakes_for_capital']} -> profit {arb['profit_pct']:.2f}% "
                       f"({self.capital * arb['profit_pct'] / 100:,.0f} SEK) whatever the result.\n"
                       f"Measured only, no bet placed. Odds move fast; check both legs before acting.")
                self.notifier.send(f"Sports arbitrage {arb['profit_pct']:.1f}% [measured]", msg, tags=["scales"])
                self.notifier.send_public(f"Sports arbitrage {arb['profit_pct']:.1f}% (measured, not bet)", msg, tags=["scales"])
        self.seen_path.write_text(json.dumps(self.seen), encoding="utf-8")
        return {"events": n_events, "arbs_se": arbs_se, "arbs_all": arbs_all}


# ---------------- report ----------------

def load(jdir: Path, name: str, fields: list[str]) -> pd.DataFrame:
    p = Path(jdir) / name
    if p.exists() and p.stat().st_size > 0:
        return pd.read_csv(p, encoding="utf-8")
    return pd.DataFrame(columns=fields)


def summarize(jdir: Path, capital: float) -> dict:
    ev = load(jdir, "events.csv", EVENT_FIELDS)
    arbs = load(jdir, "arbs.csv", ARB_FIELDS)
    scans = load(jdir, "scans.csv", SCAN_FIELDS)
    out: dict = {"scans": int(len(scans)), "events": int(len(ev)), "days": 0.0}
    if len(scans) >= 2:
        out["days"] = (int(scans["scan_ms"].iloc[-1]) - int(scans["scan_ms"].iloc[0])) / 86_400_000
    if len(ev):
        m = pd.to_numeric(ev["margin_se"], errors="coerce").dropna()
        out["median_margin_se_pct"] = float((m.median() - 1) * 100) if len(m) else None
        out["share_near_arb_pct"] = float((m < 1.01).mean() * 100) if len(m) else None
        ma = pd.to_numeric(ev["margin_all"], errors="coerce").dropna()
        out["median_margin_all_pct"] = float((ma.median() - 1) * 100) if len(ma) else None
    for label in ("se", "all"):
        a = arbs[arbs["set"] == label] if len(arbs) else arbs
        # one row per event and day, so the same arbitrage seen in two scans is not double counted
        if len(a):
            a = a.assign(day=a["scan_time"].astype(str).str[:10]).drop_duplicates(["event", "day"])
        out[f"arbs_{label}"] = int(len(a))
        out[f"avg_profit_{label}_pct"] = float(a["profit_pct"].mean()) if len(a) else 0.0
        out[f"max_profit_{label}_pct"] = float(a["profit_pct"].max()) if len(a) else 0.0
        out[f"sek_per_week_{label}"] = (float(a["profit_pct"].sum()) / 100 * capital / max(out["days"], 1) * 7) if len(a) else 0.0
        books: dict[str, int] = {}
        for legs in a["legs"].astype(str) if len(a) else []:
            for leg in legs.split(" | "):
                if "@" in leg:
                    b = leg.split("@")[-1].strip()
                    books[b] = books.get(b, 0) + 1
        out[f"books_{label}"] = dict(sorted(books.items(), key=lambda kv: -kv[1]))
    return out
