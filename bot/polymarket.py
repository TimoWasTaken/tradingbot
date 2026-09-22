"""Polymarket paper bettor: scans the public Gamma API for two documented, systematic edges and bets simulated
money on them, then measures whether the edge is real.

Edge 1, "favorites": the favorite-longshot bias. On Polymarket, contracts bought at 90 cents or more have historically
paid slightly more than their price implied (about +0.3 to +1 cent per dollar, Politics and Crypto strongest), while
longshots under 10 cents lost 6 to 20 cents per dollar. Sports is the exception (no bias), so it is excluded.
Source: "The Favorite-Longshot Bias in Prediction Markets: Evidence from Polymarket", arXiv 2609.12878.

Edge 2, "arbitrage": in mutually exclusive multi-outcome events (negRisk) the YES prices must sum to 1. When the best
asks sum to less than 1 you can buy every outcome and be paid 1 no matter what happens. Same for a binary market whose
YES ask plus NO ask is below 1. These gaps are small, rare and taken by bots within seconds, so this mostly measures
how often a slow scanner still sees one.

Everything here is simulated. The module never signs or sends an order."""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests

from .config import fmt_ms, fmt_num, now_ms

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SPORTS_WORDS = re.compile(
    r"\b(nba|nfl|mlb|nhl|mls|ufc|mma|wnba|ncaa|nascar|f1|formula 1|grand prix|premier league|la liga|serie a|"
    r"bundesliga|ligue 1|champions league|europa league|world cup|euro 20\d\d|copa|super bowl|stanley cup|"
    r"world series|playoffs?|finals?|tennis|golf|pga|masters|wimbledon|open championship|us open|french open|"
    r"boxing|fight|match|game \d|vs\.?|spread:|o/u|over/under|moneyline|touchdowns?|goals?|points|rebounds|"
    r"cricket|rugby|hockey|soccer|football|basketball|baseball|esports?|lol worlds|csgo|dota|valorant|"
    r"olympics?|medal|marathon|cycling|tour de france|darts|snooker|chess)\b", re.I)
CRYPTO_WORDS = re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|sol|crypto|dogecoin|xrp|token|airdrop|binance|coinbase)\b", re.I)
POLITICS_WORDS = re.compile(
    r"\b(election|president|presidential|senate|senator|congress|governor|parliament|prime minister|minister|"
    r"vote|ballot|primary|nominee|nomination|party|coalition|impeach|executive order|supreme court|"
    r"trump|biden|harris|vance|putin|zelensky|xi|macron|starmer|netanyahu|ceasefire|treaty|sanction|tariff|"
    r"fed|fomc|rate cut|rate hike|interest rate|cpi|inflation|gdp|recession)\b", re.I)


# ---------------- API ----------------

def _get(url: str, params: dict | None = None, tries: int = 3):
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Polymarket request failed: {url}: {last}")


def _jlist(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return []
    return []


def _fnum(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_date(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize(m: dict) -> dict | None:
    """Gamma market object -> flat dict. Returns None for anything that is not a plain Yes/No market."""
    outcomes = _jlist(m.get("outcomes"))
    prices = [_fnum(x) for x in _jlist(m.get("outcomePrices"))]
    tokens = _jlist(m.get("clobTokenIds"))
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    ev = (m.get("events") or [{}])[0] if isinstance(m.get("events"), list) else {}
    end = _parse_date(m.get("endDate"))
    best_bid = _fnum(m.get("bestBid"), 0.0)
    best_ask = _fnum(m.get("bestAsk"), 0.0)
    return {
        "id": str(m.get("id")), "question": str(m.get("question", "")), "slug": str(m.get("slug", "")),
        "event_id": str(ev.get("id", "")), "event_title": str(ev.get("title", "")), "event_slug": str(ev.get("slug", "")),
        "neg_risk": bool(m.get("negRisk") or ev.get("negRisk")),
        "outcomes": outcomes, "prices": prices, "tokens": tokens,
        "best_bid": best_bid, "best_ask": best_ask, "spread": _fnum(m.get("spread"), abs(best_ask - best_bid)),
        "tick": _fnum(m.get("orderPriceMinTickSize"), 0.001),
        "liquidity": _fnum(m.get("liquidity")), "volume": _fnum(m.get("volume")), "volume24h": _fnum(m.get("volume24hr")),
        "end": end, "closed": bool(m.get("closed")), "active": bool(m.get("active")),
        "uma": str(m.get("umaResolutionStatus", "") or ""), "group_title": str(m.get("groupItemTitle", "") or ""),
        "category": classify(str(m.get("question", "")) + " " + str(ev.get("title", "")) + " " + str(ev.get("slug", ""))),
    }


def classify(text: str) -> str:
    if SPORTS_WORDS.search(text):
        return "sports"
    if CRYPTO_WORDS.search(text):
        return "crypto"
    if POLITICS_WORDS.search(text):
        return "politics"
    return "other"


def fetch_active_markets(max_markets: int = 1500, log=print) -> list[dict]:
    """Active, open Yes/No markets sorted by 24h volume, normalized."""
    out: list[dict] = []
    offset = 0
    page = 100          # the Gamma API caps a page at 100 markets
    while len(out) < max_markets:
        raw = _get(f"{GAMMA}/markets", {"active": "true", "closed": "false", "limit": page, "offset": offset,
                                         "order": "volume24hr", "ascending": "false"})
        if not raw:
            break
        for m in raw:
            n = normalize(m)
            if n is not None:
                out.append(n)
        if len(raw) < page:
            break
        offset += page
        time.sleep(0.3)
    log(f"Fetched {len(out)} active Yes/No markets from Polymarket.")
    return out[:max_markets]


def fetch_market(market_id: str) -> dict | None:
    raw = _get(f"{GAMMA}/markets/{market_id}")
    return normalize(raw) if isinstance(raw, dict) else None


def resolution(m: dict) -> int | None:
    """Index of the winning outcome (0 = first outcome, usually Yes) or None while unresolved."""
    if not m or not m.get("closed"):
        return None
    p = m.get("prices") or []
    if len(p) == 2 and {round(p[0], 3), round(p[1], 3)} == {0.0, 1.0}:
        return 0 if p[0] > 0.5 else 1
    return None


# ---------------- scanners ----------------

def days_left(m: dict, now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(dt.timezone.utc)
    if m["end"] is None:
        return 9999.0
    return (m["end"] - now).total_seconds() / 86400.0


def side_quotes(m: dict) -> list[dict]:
    """Best ask to BUY each side. NO is the mirror of the YES book: ask_no = 1 - bid_yes."""
    if m["best_ask"] <= 0 or m["best_bid"] <= 0:
        return []
    return [
        {"side": 0, "name": m["outcomes"][0], "ask": m["best_ask"], "bid": m["best_bid"]},
        {"side": 1, "name": m["outcomes"][1], "ask": round(1 - m["best_bid"], 4), "bid": round(1 - m["best_ask"], 4)},
    ]


def scan_favorites(markets: list[dict], cfg: dict, held_ids: set[str]) -> list[dict]:
    f = cfg["favorites"]
    excluded = {c.lower() for c in f.get("exclude_categories", ["sports"])}
    out = []
    for m in markets:
        if m["id"] in held_ids or m["closed"] or not m["active"] or m["uma"] == "resolved":
            continue
        if m["category"] in excluded:
            continue
        d = days_left(m)
        if d < float(f.get("min_days", 1)) or d > float(f.get("max_days", 45)):
            continue
        if m["liquidity"] < float(f.get("min_liquidity", 20000)) or m["volume24h"] < float(f.get("min_volume24h", 1000)):
            continue
        if m["spread"] > float(f.get("max_spread", 0.01)):
            continue
        for q in side_quotes(m):
            if float(f["min_price"]) <= q["ask"] <= float(f["max_price"]):
                edge = float(f.get("assumed_edge_pct", 0.8)) / 100.0
                annual = edge * 365.0 / max(d, 1.0)
                out.append({**q, "market": m, "days": d, "kind": "favorite", "annualized_pct": annual * 100,
                            "why": (f"favorite priced {q['ask']:.3f}: contracts bought at 90c or more have historically paid "
                                    f"about +0.3 to +1c per dollar more than their price implied (favorite-longshot bias), "
                                    f"category {m['category']}, resolves in {d:.0f} days")})
                break
    out.sort(key=lambda c: (-c["annualized_pct"], -c["market"]["liquidity"]))
    return out


def scan_arbitrage(markets: list[dict], cfg: dict) -> list[dict]:
    """Two kinds of risk-free (before fees) opportunities:
    - binary: YES ask + NO ask < 1 (exactly one of them pays 1)
    - negRisk events (at most ONE outcome can resolve YES): buy NO on a set of outcomes. Cost = sum(1 - yes_bid),
      guaranteed payout = number of legs - 1, so it is profitable when the YES bids sum to more than 1.
      This does not require the outcome list to be complete, which the "buy every YES" version would.
    Each candidate: kind, legs [(market, side, ask)], cost per set, payout per set, edge (fraction of cost)."""
    a = cfg["arbitrage"]
    min_edge = float(a.get("min_edge", 0.01))
    min_liq = float(a.get("min_liquidity", 5000))
    out = []
    for m in markets:
        qs = side_quotes(m)
        if len(qs) == 2 and not m["closed"] and m["liquidity"] >= min_liq:
            total = qs[0]["ask"] + qs[1]["ask"]
            if total < 1 - min_edge:
                out.append({"kind": "arb_binary", "legs": [(m, 0, qs[0]["ask"]), (m, 1, qs[1]["ask"])], "cost": total,
                            "payout": 1.0, "edge": (1 - total) / total, "title": m["question"],
                            "why": f"YES ask {qs[0]['ask']:.3f} + NO ask {qs[1]['ask']:.3f} = {total:.3f} < 1: buying both pays 1 whatever happens"})
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        if m["neg_risk"] and m["event_id"] and not m["closed"] and m["best_bid"] > 0 and m["liquidity"] >= min_liq:
            by_event.setdefault(m["event_id"], []).append(m)
    for eid, ms in by_event.items():
        if len(ms) < 2:
            continue
        ms = sorted(ms, key=lambda x: -x["best_bid"])
        best = None
        cost = 0.0
        bids = 0.0
        for k, m in enumerate(ms, start=1):
            bids += m["best_bid"]
            cost += 1 - m["best_bid"]          # ask for NO = 1 - YES bid
            if k >= 2 and bids > 1:
                payout = k - 1
                edge = (payout - cost) / cost
                if best is None or edge > best["edge"]:
                    best = {"k": k, "edge": edge, "cost": cost, "payout": float(payout), "bids": bids}
        if best and best["edge"] >= min_edge:
            legs = [(m, 1, round(1 - m["best_bid"], 4)) for m in ms[: best["k"]]]
            out.append({"kind": "arb_negrisk", "legs": legs, "cost": best["cost"], "payout": best["payout"], "edge": best["edge"],
                        "title": ms[0]["event_title"] or ms[0]["question"],
                        "why": (f"the YES bids of {best['k']} mutually exclusive outcomes sum to {best['bids']:.3f} > 1; buying NO on "
                                f"all {best['k']} costs {best['cost']:.3f} and pays at least {best['k'] - 1} because at most one can win")})
    out.sort(key=lambda c: -c["edge"])
    return out


# ---------------- paper bettor ----------------

BET_FIELDS = ["id", "opened", "opened_ms", "kind", "category", "market_id", "event", "question", "side", "price", "qty",
              "stake", "end_date", "days", "settled", "settled_ms", "payout", "pnl", "pnl_pct", "result"]


class PaperBettor:
    def __init__(self, cfg: dict, jdir: Path, notifier, log):
        self.cfg = cfg
        self.jdir = Path(jdir)
        self.jdir.mkdir(parents=True, exist_ok=True)
        self.notifier = notifier
        self.log = log
        self.state_path = self.jdir / "state.json"
        self.state = self._load()
        self.sek_rate = float(cfg["capital"].get("sek_per_unit", 0) or 0)

    def _load(self) -> dict:
        if self.state_path.exists() and self.state_path.stat().st_size > 0:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        start = float(self.cfg["capital"]["start"])
        return {"start": start, "cash": start, "positions": [], "bet_counter": 0, "last_snapshot_day": "",
                "report_day": "", "last_scan_ms": 0}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    # ---------- helpers ----------
    def money(self, x: float) -> str:
        return f"{fmt_num(x, 2)} USD" + (f" (~{fmt_num(x * self.sek_rate, 0)} SEK)" if self.sek_rate > 0 else "")

    def _append_csv(self, name: str, fields: list[str], row: dict) -> None:
        p = self.jdir / name
        new = not p.exists() or p.stat().st_size == 0
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def held_ids(self) -> set[str]:
        return {p["market_id"] for p in self.state["positions"]}

    def equity(self, price_lookup: dict[str, list[float]] | None = None) -> float:
        total = float(self.state["cash"])
        for p in self.state["positions"]:
            px = p["price"]
            if price_lookup and p["market_id"] in price_lookup:
                px = price_lookup[p["market_id"]][int(p["side"])]
            total += float(p["qty"]) * float(px)
        return total

    # ---------- betting ----------
    def place(self, m: dict, side: int, price: float, stake: float, kind: str, why: str, quiet: bool = False) -> dict | None:
        tick = float(m.get("tick") or 0.001)
        fill = min(round(price + tick, 4), 0.999)          # assume one tick of slippage
        if stake > self.state["cash"] or stake < float(self.cfg.get("min_stake", 2.0)):
            return None
        qty = stake / fill
        self.state["bet_counter"] += 1
        d = days_left(m)
        pos = {"id": self.state["bet_counter"], "opened": fmt_ms(now_ms()), "opened_ms": now_ms(), "kind": kind,
               "category": m["category"], "market_id": m["id"], "event_id": m["event_id"], "event": m["event_title"],
               "question": m["question"],
               "side": int(side), "side_name": m["outcomes"][side], "price": fill, "qty": qty, "stake": stake,
               "end_date": m["end"].strftime("%Y-%m-%d") if m["end"] else "", "days": round(d, 1), "why": why}
        self.state["cash"] -= stake
        self.state["positions"].append(pos)
        msg = (f"BUY {pos['side_name']} on \"{m['question']}\" at {fill:.3f}, {self.money(stake)} for {qty:.1f} shares. "
               f"Pays {self.money(qty)} if right, 0 if wrong. Resolves around {pos['end_date']}.\nWhy: {why}.")
        self.log(f"BET #{pos['id']}: " + msg.replace("\n", " "))
        if not quiet:
            self.notifier.send_both(f"Polymarket paper bet #{pos['id']} [paper]", msg, tags=["game_die"])
        return pos

    def place_favorite(self, c: dict) -> dict | None:
        f = self.cfg["favorites"]
        open_fav = [p for p in self.state["positions"] if p["kind"] == "favorite"]
        if len(open_fav) >= int(f.get("max_open", 8)):
            return None
        m = c["market"]
        # spread the bets: the September Bitcoin markets are all one bet on the same thing
        if sum(1 for p in open_fav if p.get("event_id") == m["event_id"]) >= int(f.get("max_per_event", 2)):
            return None
        if sum(1 for p in open_fav if p.get("category") == m["category"]) >= int(f.get("max_per_category", 4)):
            return None
        stake = min(self.equity() * float(f.get("stake_pct", 10)) / 100.0, float(self.state["cash"]))
        return self.place(c["market"], c["side"], c["ask"], round(stake, 2), "favorite", c["why"])

    def place_arbitrage(self, c: dict) -> list[dict]:
        """All legs or nothing: a partial set is not an arbitrage."""
        a = self.cfg["arbitrage"]
        held = self.held_ids()
        if any(m["id"] in held for m, _s, _ask in c["legs"]):
            return []
        budget = min(self.equity() * float(a.get("stake_pct", 20)) / 100.0, float(self.state["cash"]))
        sets = budget / c["cost"]                              # number of complete sets we can afford
        min_stake = float(self.cfg.get("min_stake", 2.0))
        smallest_leg = min(ask for _m, _s, ask in c["legs"])
        if sets * smallest_leg < min_stake:                    # every leg must be a real bet
            sets = min_stake / smallest_leg
            if sets * c["cost"] > self.state["cash"]:
                return []
        placed = []
        for m, side, ask in c["legs"]:
            pos = self.place(m, side, ask, round(sets * ask, 2), c["kind"], c["why"], quiet=True)
            if pos is None:                                    # roll back, keep the book consistent
                for p in placed:
                    self.state["cash"] += p["stake"]
                    self.state["positions"].remove(p)
                return []
            placed.append(pos)
        total_cost = sum(p["stake"] for p in placed)
        payout = sets * c["payout"]
        msg = (f"{len(placed)} legs on \"{c['title']}\" for {self.money(total_cost)} in total, guaranteed payout "
               f"{self.money(payout)} = {(payout / total_cost - 1) * 100:.1f}% locked in (before slippage on the fills).\n"
               f"Why: {c['why']}.")
        self.log("ARBITRAGE: " + msg.replace("\n", " "))
        self.notifier.send_both("Polymarket arbitrage (paper)", msg, tags=["money_with_wings"])
        return placed

    # ---------- settlement ----------
    def settle(self) -> None:
        keep = []
        for p in self.state["positions"]:
            try:
                m = fetch_market(p["market_id"])
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not check bet #{p['id']}: {e}")
                keep.append(p)
                continue
            win = resolution(m) if m else None
            if win is None:
                keep.append(p)
                continue
            payout = float(p["qty"]) if win == int(p["side"]) else 0.0
            pnl = payout - float(p["stake"])
            self.state["cash"] += payout
            row = {**p, "settled": fmt_ms(now_ms()), "settled_ms": now_ms(), "payout": round(payout, 4),
                   "pnl": round(pnl, 4), "pnl_pct": round(pnl / p["stake"] * 100, 3), "result": "won" if payout > 0 else "lost"}
            self._append_csv("bets.csv", BET_FIELDS, row)
            outcome = "WON" if payout > 0 else "LOST"
            msg = (f"{outcome}: {p['side_name']} on \"{p['question']}\" bought at {p['price']:.3f}. "
                   f"Result {'+' if pnl >= 0 else '-'}{self.money(abs(pnl))} ({pnl / p['stake'] * 100:+.1f}%).\n"
                   f"Bankroll now {self.money(self.equity())}.")
            self.log(f"SETTLED #{p['id']}: " + msg.replace("\n", " "))
            self.notifier.send_both(f"Polymarket bet #{p['id']} {outcome.lower()} [paper]", msg,
                                    tags=["white_check_mark" if payout > 0 else "x"])
        self.state["positions"] = keep

    # ---------- research snapshots ----------
    def snapshot(self, markets: list[dict]) -> None:
        """Once a day: record every scanned market's price so the favorite-longshot bias can be measured on our own data."""
        day = dt.datetime.now().strftime("%Y-%m-%d")
        if self.state.get("last_snapshot_day") == day:
            return
        fields = ["day", "market_id", "question", "category", "yes_price", "liquidity", "volume24h", "end_date", "neg_risk"]
        for m in markets:
            self._append_csv("snapshots.csv", fields, {
                "day": day, "market_id": m["id"], "question": m["question"], "category": m["category"],
                "yes_price": m["prices"][0] if m["prices"] else "", "liquidity": round(m["liquidity"]),
                "volume24h": round(m["volume24h"]), "end_date": m["end"].strftime("%Y-%m-%d") if m["end"] else "",
                "neg_risk": int(m["neg_risk"])})
        self.state["last_snapshot_day"] = day

    # ---------- one cycle ----------
    def cycle(self) -> dict:
        markets = fetch_active_markets(int(self.cfg.get("scan_limit", 1500)), log=self.log)
        lookup = {m["id"]: m["prices"] for m in markets}
        self.settle()
        summary = {"favorites": [], "arbs": []}
        if self.cfg["arbitrage"].get("enabled", True):
            for c in scan_arbitrage(markets, self.cfg)[:3]:
                if self.place_arbitrage(c):
                    summary["arbs"].append(c)
        if self.cfg["favorites"].get("enabled", True):
            for c in scan_favorites(markets, self.cfg, self.held_ids()):
                if self.place_favorite(c):
                    summary["favorites"].append(c)
        self.snapshot(markets)
        eq = self.equity(lookup)
        self._append_csv("equity.csv", ["time", "ms", "equity", "cash"],
                         {"time": fmt_ms(now_ms()), "ms": now_ms(), "equity": round(eq, 4), "cash": round(self.state["cash"], 4)})
        self.state["last_scan_ms"] = now_ms()
        self.log(f"Bankroll {self.money(eq)} ({(eq / self.state['start'] - 1) * 100:+.2f}% since start) | cash "
                 f"{fmt_num(self.state['cash'])} USD | open bets: {len(self.state['positions'])} | "
                 f"new this cycle: {len(summary['favorites'])} favorites, {len(summary['arbs'])} arbitrages")
        self.daily_report(eq)
        self.save()
        return summary

    def daily_report(self, eq: float) -> None:
        hour = int(self.cfg.get("ntfy", {}).get("daily_report_hour", 21))
        now = dt.datetime.now()
        day = now.strftime("%Y-%m-%d")
        if now.hour < hour or self.state.get("report_day") == day:
            return
        self.state["report_day"] = day
        bets = load_bets(self.jdir)
        n = len(bets)
        lines = [f"Bankroll {self.money(eq)} ({(eq / self.state['start'] - 1) * 100:+.1f}% since start)."]
        if n:
            won = int((bets["result"] == "won").sum())
            lines.append(f"Settled: {n} bets, {won} won ({won / n * 100:.0f}%), average {bets['pnl_pct'].mean():+.2f}% per bet.")
        else:
            lines.append("No settled bets yet.")
        lines.append(f"Open bets: {len(self.state['positions'])}: " +
                     (", ".join(f"{p['side_name']} {p['question'][:40]} @ {p['price']:.2f}" for p in self.state["positions"][:6]) or "none"))
        self.notifier.send("Daily report: Polymarket bot [paper]", "\n".join(lines), tags=["bar_chart"])


def load_bets(jdir: Path) -> pd.DataFrame:
    p = Path(jdir) / "bets.csv"
    if p.exists() and p.stat().st_size > 0:
        return pd.read_csv(p, encoding="utf-8")
    return pd.DataFrame(columns=BET_FIELDS)


def load_equity(jdir: Path) -> pd.DataFrame:
    p = Path(jdir) / "equity.csv"
    if p.exists() and p.stat().st_size > 0:
        return pd.read_csv(p, encoding="utf-8")
    return pd.DataFrame(columns=["time", "ms", "equity", "cash"])


# ---------------- research: measure the bias on our own snapshots ----------------

def research(jdir: Path, log=print, max_lookups: int = 400) -> pd.DataFrame:
    """Joins daily snapshots with resolutions and reports realized return per dollar by price bucket and category.
    Resolutions are cached in resolutions.csv so repeated runs are cheap."""
    jdir = Path(jdir)
    sp = jdir / "snapshots.csv"
    if not sp.exists():
        log("No snapshots yet. The bot records one per market per day while it runs.")
        return pd.DataFrame()
    snaps = pd.read_csv(sp, encoding="utf-8")
    rp = jdir / "resolutions.csv"
    res = pd.read_csv(rp, encoding="utf-8") if rp.exists() and rp.stat().st_size > 0 else pd.DataFrame(columns=["market_id", "winner"])
    known = set(res["market_id"].astype(str))
    today = dt.date.today()
    todo = [str(x) for x in snaps.loc[pd.to_datetime(snaps["end_date"], errors="coerce").dt.date < today, "market_id"].unique()
            if str(x) not in known][:max_lookups]
    new_rows = []
    for i, mid in enumerate(todo):
        try:
            m = fetch_market(mid)
            w = resolution(m) if m else None
            if w is not None:
                new_rows.append({"market_id": mid, "winner": w})
        except Exception as e:  # noqa: BLE001
            log(f"lookup failed for {mid}: {e}")
        if i % 25 == 24:
            log(f"  looked up {i + 1} of {len(todo)} resolutions ...")
        time.sleep(0.2)
    if new_rows:
        res = pd.concat([res, pd.DataFrame(new_rows)], ignore_index=True)
        res.to_csv(rp, index=False)
    if res.empty:
        log("No resolved markets among the snapshots yet.")
        return pd.DataFrame()
    snaps["market_id"] = snaps["market_id"].astype(str)
    res["market_id"] = res["market_id"].astype(str)
    df = snaps.merge(res, on="market_id", how="inner")
    df["yes_price"] = pd.to_numeric(df["yes_price"], errors="coerce")
    df = df.dropna(subset=["yes_price"])
    # return per dollar of buying YES at the snapshot price and holding to resolution
    df["ret_yes"] = (df["winner"].astype(int) == 0).astype(float) / df["yes_price"] - 1.0
    bins = [0, 0.05, 0.10, 0.20, 0.40, 0.60, 0.80, 0.90, 0.95, 1.0001]
    labels = ["0-5c", "5-10c", "10-20c", "20-40c", "40-60c", "60-80c", "80-90c", "90-95c", "95-100c"]
    df["bucket"] = pd.cut(df["yes_price"], bins=bins, labels=labels, right=False)
    table = df.groupby(["bucket"], observed=True).agg(n=("ret_yes", "size"), avg_return_pct=("ret_yes", lambda s: s.mean() * 100),
                                                        win_rate_pct=("winner", lambda s: (s == 0).mean() * 100)).reset_index()
    log("\nRealized return per dollar for buying YES at the snapshot price (own data):")
    log(table.to_string(index=False))
    by_cat = df.groupby(["category", "bucket"], observed=True).agg(n=("ret_yes", "size"), avg_return_pct=("ret_yes", lambda s: s.mean() * 100)).reset_index()
    log("\nBy category:")
    log(by_cat.to_string(index=False))
    return table
