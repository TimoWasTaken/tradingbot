"""Crypto 15-minute markets on Polymarket: the speed test (paper money only).

Every 15 minutes Polymarket opens "Bitcoin Up or Down" and "Ethereum Up or Down" markets that pay 1 USDC to UP when
the Chainlink price at the end of the window is at or above the price at its start, otherwise to DOWN. The bots that
demonstrably make money on Polymarket mostly live here: they watch the spot price on Binance, which moves first, and
buy the side Polymarket has not repriced yet. Polymarket answered with a taker fee on these markets
(shares x 0.07 x price x (1 - price), 1.75 cents per share at 50 cents) that is paid out to the market makers.

This module measures how much of that edge is reachable at the speed a home computer polling public APIs has,
one look every few seconds, and only with simulated money:

1. Fair-value strategy. Each poll it turns the Binance move since the window opened, the time left and the realised
   volatility of the last hour into the probability that the window ends UP (a normal-distribution estimate). When a
   side's best ask is far enough below that probability, after the taker fee and one tick of slippage, it buys the
   side on paper and holds to resolution. One bet per window and coin.
2. DipArb replay. Public "Polymarket bot" repositories buy a side whose ask fell 15 % within a few seconds and then
   try to buy the other side within 60 s so that the pair costs less than 0.92 and pays 1. Every such dip is replayed
   with the same rules and logged with what happened next (hedged, stopped out, or held to resolution), so that
   strategy's real frequency and result can be read off the journal.
3. Ticks. Binance price, the Polymarket books and the model probability are written every few seconds so the
   calibration of the market and of the model can be measured afterwards (the research command).

Nothing here sends an order. The journal is the product."""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import time
from collections import deque
from pathlib import Path

import requests

from .config import fmt_ms, fmt_num, now_ms
from .polymarket import GAMMA, _fnum, _get, _jlist, fetch_books, taker_fee

BINANCE = "https://api.binance.com/api/v3"
SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
ROUND_S = 900

TICK_FIELDS = ["time", "ms", "coin", "round", "t_left", "px", "open_px", "move_pct", "sigma_15m_pct", "fair_up",
               "up_bid", "up_ask", "up_ask_size", "down_bid", "down_ask", "down_ask_size", "sum_asks"]
BET_FIELDS = ["id", "opened", "opened_ms", "kind", "coin", "round", "side", "price", "qty", "stake", "fee", "fair", "t_left",
              "settled", "settled_ms", "winner", "payout", "pnl", "pnl_pct", "result", "resolved_by"]
DIP_FIELDS = ["time", "ms", "coin", "round", "side", "from_ask", "to_ask", "drop_pct", "t_left", "sum_at_dip", "min_sum",
              "outcome", "hedge_after_s", "shares", "cost", "proceeds", "pnl"]
ROUND_FIELDS = ["coin", "round", "start", "end", "open_px", "open_lag_s", "close_px", "winner", "resolved_by", "market_id"]
EQ_FIELDS = ["time", "ms", "equity", "cash", "dip_equity", "dip_cash"]


# ---------------- data ----------------

def round_start(ts: float | None = None) -> int:
    ts = time.time() if ts is None else ts
    return int(ts // ROUND_S * ROUND_S)


def fetch_round(coin: str, start: int) -> dict | None:
    """The Gamma market for one 15-minute window, or None if Polymarket has not created it (yet)."""
    raw = _get(f"{GAMMA}/markets", {"slug": f"{coin}-updown-15m-{start}"})
    if not raw or not isinstance(raw, list):
        return None
    m = raw[0]
    tokens = [str(t) for t in _jlist(m.get("clobTokenIds"))]
    outcomes = [str(o) for o in _jlist(m.get("outcomes"))]
    if len(tokens) != 2 or len(outcomes) != 2:
        return None
    up = 0 if outcomes[0].lower().startswith("up") else 1
    return {"id": str(m.get("id")), "slug": str(m.get("slug", "")), "question": str(m.get("question", "")),
            "up_token": tokens[up], "down_token": tokens[1 - up], "up_index": up,
            "fee_rate": 0.07 if m.get("feesEnabled") else 0.0, "start": start, "end": start + ROUND_S}


def fetch_winner(market_id: str, up_index: int) -> str | None:
    raw = _get(f"{GAMMA}/markets/{market_id}")
    if not isinstance(raw, dict) or not raw.get("closed"):
        return None
    prices = [_fnum(x) for x in _jlist(raw.get("outcomePrices"))]
    if len(prices) == 2 and {round(prices[0], 3), round(prices[1], 3)} == {0.0, 1.0}:
        return "Up" if prices[up_index] > 0.5 else "Down"
    return None


def binance_prices(symbols: list[str]) -> dict[str, float]:
    r = requests.get(f"{BINANCE}/ticker/price", params={"symbols": json.dumps(symbols, separators=(",", ":"))}, timeout=10)
    r.raise_for_status()
    return {x["symbol"]: float(x["price"]) for x in r.json()}


def binance_sigma_per_s(symbol: str, minutes: int = 60) -> float:
    """Realised volatility of 1-minute log returns over the last `minutes`, scaled to one second."""
    r = requests.get(f"{BINANCE}/klines", params={"symbol": symbol, "interval": "1m", "limit": max(minutes, 10)}, timeout=10)
    r.raise_for_status()
    closes = [float(k[4]) for k in r.json()]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 5:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var) / math.sqrt(60.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_up(move: float, sigma_s: float, t_left: float) -> float:
    """P(price at the end >= price at the start | log move so far, seconds left, per-second volatility)."""
    if t_left <= 0:
        return 1.0 if move >= 0 else 0.0
    s = sigma_s * math.sqrt(t_left)
    if s <= 0:
        return 1.0 if move >= 0 else 0.0
    return norm_cdf(move / s)


# ---------------- the watcher ----------------

class CryptoWatcher:
    def __init__(self, cfg: dict, jdir: Path, notifier, log):
        self.cfg = cfg
        self.jdir = Path(jdir)
        self.jdir.mkdir(parents=True, exist_ok=True)
        self.notifier = notifier
        self.log = log
        self.coins = [c for c in cfg.get("coins", ["btc", "eth"]) if c in SYMBOLS]
        self.tick = float(cfg.get("tick", 0.01))
        self.state_path = self.jdir / "state.json"
        self.state = self._load()
        self.sek_rate = float(cfg["capital"].get("sek_per_unit", 0) or 0)
        self.rounds: dict[str, dict] = {}            # coin -> live round (market, open price, histories)
        self.sigma: dict[str, tuple[float, float]] = {}   # coin -> (fetched at, sigma per second)
        self.last_tick_log: dict[str, float] = {}
        self.last_settle = 0.0
        self.last_save = 0.0
        self.recovered = False
        self.restore_dips: dict[str, dict] = {}

    def _load(self) -> dict:
        if self.state_path.exists() and self.state_path.stat().st_size > 0:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        start = float(self.cfg["capital"]["start"])
        dip_start = float(self.cfg.get("diparb", {}).get("start_cash", start))
        return {"start": start, "cash": start, "dip_start": dip_start, "dip_cash": dip_start, "positions": [], "bet_counter": 0,
                "pending_rounds": [], "ticks": 0, "dips": 0, "dips_hedged": 0, "report_day": dt.datetime.now().strftime("%Y-%m-%d"), "last_eq_ms": 0,
                "since": fmt_ms(now_ms())}

    def save(self, force: bool = True) -> None:
        if not force and time.time() - self.last_save < 60:
            return
        self.state["active_dips"] = {c: r["dip"] for c, r in self.rounds.items() if r.get("dip")}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.state_path)
        self.last_save = time.time()

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

    # ---------- equity ----------
    def equity(self, book: str, books: dict | None = None) -> float:
        total = float(self.state["cash"] if book == "fair" else self.state["dip_cash"])
        for p in self.state["positions"]:
            if p["book"] != book:
                continue
            px = float(p["price"])
            r = self.rounds.get(p["coin"])
            if books and r and r.get("market") and r["round"] == p["round"]:
                tok = r["market"]["up_token"] if p["side"] == "Up" else r["market"]["down_token"]
                b = books.get(tok)
                if b and b["bid"] > 0:
                    px = b["bid"]
            total += float(p["qty"]) * px
        return total

    # ---------- rounds ----------
    def _sigma_for(self, coin: str, now: float) -> float:
        got = self.sigma.get(coin)
        if got and now - got[0] < 300:
            return got[1]
        try:
            s = binance_sigma_per_s(SYMBOLS[coin], int(self.cfg.get("fair", {}).get("vol_minutes", 60)))
        except Exception as e:  # noqa: BLE001
            self.log(f"{coin}: volatility fetch failed: {e}")
            s = got[1] if got else 0.0
        self.sigma[coin] = (now, s)
        return s

    def _roll_round(self, coin: str, rs: int, now: float, px: float) -> None:
        old = self.rounds.get(coin)
        if old:
            if old.get("market"):
                self.state["pending_rounds"].append({
                    "coin": coin, "round": old["round"], "market_id": old["market"]["id"], "up_index": old["market"]["up_index"],
                    "open_px": old["open_px"], "open_lag_s": old["open_lag_s"], "close_px": old.get("last_px", px), "end": old["end"],
                    "last_check": 0.0})
            dip = old.get("dip")
            if dip:                                   # window ended with an unhedged leg: it rides to resolution
                self._hold_dip(old, dip)
        market = None
        try:
            market = fetch_round(coin, rs)
        except Exception as e:  # noqa: BLE001
            self.log(f"{coin}: could not fetch the {fmt_ms(rs * 1000, seconds=True)} window: {e}")
        lag = now - rs
        self.rounds[coin] = {"round": rs, "end": rs + ROUND_S, "market": market, "open_px": px, "open_lag_s": round(lag, 1),
                             "last_px": px, "hist": deque(maxlen=120), "dip": None, "bet_done": False, "last_try": now}
        if coin in self.restore_dips and self.restore_dips[coin].get("round") == rs:
            self.rounds[coin]["dip"] = self.restore_dips.pop(coin)
            self.log(f"{coin}: restored the open DipArb leg from before the restart.")
        if market is None:
            self.log(f"{coin}: no Polymarket market yet for the window starting {fmt_ms(rs * 1000, seconds=True)}, retrying.")

    def _hold_dip(self, r: dict, dip: dict) -> None:
        self.state["bet_counter"] += 1
        self.state["positions"].append({
            "id": self.state["bet_counter"], "opened": fmt_ms(dip["start_ms"], seconds=True), "opened_ms": dip["start_ms"],
            "kind": "diparb_leg1", "book": "dip", "coin": dip["coin"], "round": dip["round"], "side": dip["side"],
            "price": dip["leg1"], "qty": dip["shares"], "stake": dip["cost"], "fee": dip["fee"], "fair": "", "t_left": dip["t_left"],
            "market_id": dip.get("market_id", ""), "up_index": dip.get("up_index", 0)})
        self._write_dip(dip, "held_to_resolution", None, None)
        if r is not None:
            r["dip"] = None

    def _recover(self, now: float) -> None:
        """After a restart: dips and bets from windows that ended while the watcher was down still need settling."""
        self.recovered = True
        cur = round_start(now)
        for coin, dip in list((self.state.get("active_dips") or {}).items()):
            if dip.get("round") == cur:
                self.restore_dips[coin] = dip
            else:
                self._hold_dip(None, dip)
        self.state["active_dips"] = {}
        pend = {(p["coin"], p["round"]) for p in self.state["pending_rounds"]}
        for pos in self.state["positions"]:
            key = (pos["coin"], pos["round"])
            if pos["round"] < cur and key not in pend and pos.get("market_id"):
                self.state["pending_rounds"].append({
                    "coin": pos["coin"], "round": pos["round"], "market_id": pos["market_id"], "up_index": int(pos.get("up_index", 0)),
                    "open_px": 0.0, "open_lag_s": -1, "close_px": 0.0, "end": int(pos["round"]) + ROUND_S, "last_check": 0.0})
                pend.add(key)
                self.log(f"{pos['coin']}: bet #{pos['id']} from a window that ended while the watcher was down will be settled from Polymarket.")

    # ---------- one poll ----------
    def poll(self) -> None:
        now = time.time()
        if not self.recovered:
            self._recover(now)
        try:
            prices = binance_prices([SYMBOLS[c] for c in self.coins])
        except Exception as e:  # noqa: BLE001
            self.log(f"Binance price fetch failed: {e}")
            return
        for coin in self.coins:
            px = prices.get(SYMBOLS[coin])
            if not px:
                continue
            rs = round_start(now)
            r = self.rounds.get(coin)
            if r is None or r["round"] != rs:
                self._roll_round(coin, rs, now, px)
                r = self.rounds[coin]
            if r["market"] is None and now - r["last_try"] >= 15:
                r["last_try"] = now
                try:
                    r["market"] = fetch_round(coin, rs)
                except Exception as e:  # noqa: BLE001
                    self.log(f"{coin}: market fetch failed: {e}")
        tokens = [t for c in self.coins for r in [self.rounds.get(c)] if r and r.get("market")
                  for t in (r["market"]["up_token"], r["market"]["down_token"])]
        books = {}
        if tokens:
            try:
                books = fetch_books(tokens)
            except Exception as e:  # noqa: BLE001
                self.log(f"Order books failed: {e}")
        for coin in self.coins:
            r = self.rounds.get(coin)
            px = prices.get(SYMBOLS[coin])
            if r and r.get("market") and px:
                try:
                    self._tick_coin(coin, r, now, px, books)
                except Exception as e:  # noqa: BLE001
                    self.log(f"{coin}: tick error: {e}")
        if now - self.last_settle >= 60:
            self.last_settle = now
            self._settle(now)
        if now_ms() - int(self.state.get("last_eq_ms", 0)) >= int(self.cfg.get("equity_seconds", 300)) * 1000:
            self._equity_row(books)
        self._daily_report(books)
        self.save(force=False)

    def _tick_coin(self, coin: str, r: dict, now: float, px: float, books: dict) -> None:
        m = r["market"]
        up, dn = books.get(m["up_token"]), books.get(m["down_token"])
        if not up or not dn or up["ask"] <= 0 or dn["ask"] <= 0:
            return
        t_left = r["end"] - now
        r["last_px"] = px
        move = math.log(px / r["open_px"]) if r["open_px"] > 0 else 0.0
        sigma = self._sigma_for(coin, now)
        fair = fair_up(move, sigma, t_left)
        ms = now_ms()
        self.state["ticks"] = int(self.state.get("ticks", 0)) + 1
        if now - self.last_tick_log.get(coin, 0) >= float(self.cfg.get("tick_log_seconds", 10)):
            self.last_tick_log[coin] = now
            self._append_csv("ticks.csv", TICK_FIELDS, {
                "time": fmt_ms(ms, seconds=True), "ms": ms, "coin": coin, "round": r["round"], "t_left": round(t_left), "px": px,
                "open_px": r["open_px"], "move_pct": round(move * 100, 4), "sigma_15m_pct": round(sigma * math.sqrt(ROUND_S) * 100, 4),
                "fair_up": round(fair, 4), "up_bid": up["bid"], "up_ask": up["ask"], "up_ask_size": round(up["ask_size"], 1),
                "down_bid": dn["bid"], "down_ask": dn["ask"], "down_ask_size": round(dn["ask_size"], 1),
                "sum_asks": round(up["ask"] + dn["ask"], 3)})
        self._fair_strategy(coin, r, m, t_left, fair, up, dn, ms)
        self._dip_strategy(coin, r, m, t_left, up, dn, ms)

    # ---------- strategy 1: fair value ----------
    def _fair_strategy(self, coin: str, r: dict, m: dict, t_left: float, fair: float, up: dict, dn: dict, ms: int) -> None:
        f = self.cfg.get("fair", {})
        if not f.get("enabled", True) or r["bet_done"] or r["open_lag_s"] > float(f.get("max_open_lag_s", 20)):
            return
        if not float(f.get("min_t_left_s", 60)) <= t_left <= float(f.get("max_t_left_s", 840)):
            return
        rate = m["fee_rate"]
        for side, ask, size, prob in (("Up", up["ask"], up["ask_size"], fair), ("Down", dn["ask"], dn["ask_size"], 1 - fair)):
            if ask < float(f.get("min_ask", 0.03)) or ask > float(f.get("max_ask", 0.97)):
                continue
            fill = min(ask + self.tick, 0.99)
            cost_per_share = fill + rate * fill * (1 - fill)
            edge = prob - cost_per_share
            if edge < float(f.get("min_edge", 0.04)):
                continue
            stake = min(self.equity("fair") * float(f.get("stake_pct", 5)) / 100.0, float(self.state["cash"]))
            if stake < float(self.cfg.get("min_stake", 1.0)):
                return
            qty = min(stake / cost_per_share, size)                # cannot buy more than the level holds
            if qty * cost_per_share < float(self.cfg.get("min_stake", 1.0)):
                return
            stake = qty * cost_per_share
            fee = taker_fee(fill, qty, rate)
            self.state["bet_counter"] += 1
            self.state["cash"] -= stake
            pos = {"id": self.state["bet_counter"], "opened": fmt_ms(ms, seconds=True), "opened_ms": ms, "kind": "fair", "book": "fair",
                   "coin": coin, "round": r["round"], "side": side, "price": round(fill, 4), "qty": round(qty, 4), "stake": round(stake, 4),
                   "fee": round(fee, 4), "fair": round(prob, 4), "t_left": round(t_left), "market_id": m["id"], "up_index": m["up_index"]}
            self.state["positions"].append(pos)
            r["bet_done"] = True
            self.log(f"BET #{pos['id']} {coin.upper()} {side} at {fill:.2f} ({qty:.1f} shares, {self.money(stake)}, fee {fee:.3f}): "
                     f"model says {prob:.2f}, Binance moved {r['last_px'] / r['open_px'] * 100 - 100:+.3f}% with {t_left:.0f}s left.")
            self.save()
            return

    # ---------- strategy 2: DipArb replay ----------
    def _dip_strategy(self, coin: str, r: dict, m: dict, t_left: float, up: dict, dn: dict, ms: int) -> None:
        d = self.cfg.get("diparb", {})
        if not d.get("enabled", True):
            return
        hist: deque = r["hist"]
        hist.append((ms, up["ask"], dn["ask"]))
        rate = m["fee_rate"]
        dip = r.get("dip")
        if dip is None:
            window = float(d.get("window_s", 5)) * 1000
            old = None
            for h in hist:                                   # the newest sample at least `window` old
                if ms - h[0] >= window:
                    old = h
                else:
                    break
            if old is None:
                return
            drop = float(d.get("drop_pct", 15)) / 100.0
            for side, old_ask, ask, other_ask, size in (("Up", old[1], up["ask"], dn["ask"], up["ask_size"]),
                                                          ("Down", old[2], dn["ask"], up["ask"], dn["ask_size"])):
                if old_ask >= 0.05 and 0 < ask <= old_ask * (1 - drop):
                    shares = float(d.get("shares", 20))
                    if size < shares:                     # the level would not fill the order
                        continue
                    fill = min(ask + self.tick, 0.99)
                    fee = taker_fee(fill, shares, rate)
                    cost = shares * fill + fee
                    if cost > self.state["dip_cash"]:
                        return
                    self.state["dip_cash"] -= cost
                    self.state["dips"] = int(self.state.get("dips", 0)) + 1
                    r["dip"] = {"coin": coin, "round": r["round"], "side": side, "from_ask": old_ask, "to_ask": ask, "leg1": fill,
                                "shares": shares, "fee": fee, "cost": cost, "start_ms": ms, "t_left": round(t_left),
                                "market_id": m["id"], "up_index": m["up_index"],
                                "sum_at_dip": round(fill + other_ask + self.tick, 3), "min_sum": round(fill + other_ask + self.tick, 3)}
                    self.log(f"DIP {coin.upper()} {side}: ask {old_ask:.2f} -> {ask:.2f} ({(1 - ask / old_ask) * 100:.0f}% in "
                             f"{window / 1000:.0f}s) with {t_left:.0f}s left. Replaying DipArb: bought {shares:.0f} shares at {fill:.2f}, "
                             f"now waiting up to {d.get('hedge_window_s', 60)}s for the other side at <= {d.get('sum_target', 0.92)} in total.")
                    self.save()
                    return
            return
        other = dn if dip["side"] == "Up" else up
        mine = up if dip["side"] == "Up" else dn
        fill2 = min(other["ask"] + self.tick, 0.99)
        total = dip["leg1"] + fill2
        dip["min_sum"] = min(dip["min_sum"], round(total, 3))
        elapsed = (ms - dip["start_ms"]) / 1000.0
        if total <= float(d.get("sum_target", 0.92)) and other["ask_size"] >= dip["shares"]:
            fee2 = taker_fee(fill2, dip["shares"], rate)
            cost2 = dip["shares"] * fill2 + fee2
            self.state["dip_cash"] -= cost2
            self.state["dip_cash"] += dip["shares"]                # a full pair pays 1 per share at resolution, whatever happens
            self.state["dips_hedged"] = int(self.state.get("dips_hedged", 0)) + 1
            pnl = dip["shares"] - dip["cost"] - cost2
            self._write_dip(dip, "hedged", elapsed, dip["shares"], pnl, cost2)
            self.log(f"DIP {coin.upper()} hedged after {elapsed:.0f}s: pair costs {total:.3f}, locked {pnl:+.2f} USD.")
            r["dip"] = None
            self.save()
        elif elapsed >= float(d.get("hedge_window_s", 60)):
            fill = max(mine["bid"] - self.tick, 0.01)
            proceeds = dip["shares"] * fill - taker_fee(fill, dip["shares"], rate)
            self.state["dip_cash"] += proceeds
            pnl = proceeds - dip["cost"]
            self._write_dip(dip, "stopped", elapsed, proceeds, pnl)
            self.log(f"DIP {coin.upper()} stopped out after {elapsed:.0f}s: best pair price was {dip['min_sum']:.3f}, sold at {fill:.2f}, "
                     f"{pnl:+.2f} USD.")
            r["dip"] = None
            self.save()

    def _write_dip(self, dip: dict, outcome: str, after_s, proceeds, pnl=None, cost2: float = 0.0) -> None:
        self._append_csv("dips.csv", DIP_FIELDS, {
            "time": fmt_ms(dip["start_ms"], seconds=True), "ms": dip["start_ms"], "coin": dip["coin"], "round": dip["round"],
            "side": dip["side"], "from_ask": dip["from_ask"], "to_ask": dip["to_ask"],
            "drop_pct": round((1 - dip["to_ask"] / dip["from_ask"]) * 100, 1), "t_left": dip["t_left"], "sum_at_dip": dip["sum_at_dip"],
            "min_sum": dip["min_sum"], "outcome": outcome, "hedge_after_s": round(after_s, 1) if after_s is not None else "",
            "shares": dip["shares"], "cost": round(dip["cost"] + cost2, 4),
            "proceeds": round(proceeds, 4) if proceeds is not None else "", "pnl": round(pnl, 4) if pnl is not None else ""})

    # ---------- settlement ----------
    def _settle(self, now: float) -> None:
        max_wait = float(self.cfg.get("settle", {}).get("max_wait_s", 7200))
        keep = []
        for pr in self.state["pending_rounds"]:
            if now - pr["end"] < 20 or now - pr.get("last_check", 0) < 60:
                keep.append(pr)
                continue
            pr["last_check"] = now
            winner, how = None, ""
            try:
                winner = fetch_winner(pr["market_id"], int(pr["up_index"]))
                how = "polymarket"
            except Exception as e:  # noqa: BLE001
                self.log(f"{pr['coin']}: resolution lookup failed: {e}")
            if winner is None and now - pr["end"] >= max_wait and float(pr.get("open_px") or 0) > 0:
                winner, how = ("Up" if pr["close_px"] >= pr["open_px"] else "Down"), "binance_proxy"
            if winner is None and now - pr["end"] >= 86400:
                winner, how = "unresolved", "refund"
            if winner is None:
                keep.append(pr)
                continue
            self._append_csv("rounds.csv", ROUND_FIELDS, {
                "coin": pr["coin"], "round": pr["round"], "start": fmt_ms(pr["round"] * 1000, seconds=True),
                "end": fmt_ms(pr["end"] * 1000, seconds=True), "open_px": pr["open_px"], "open_lag_s": pr["open_lag_s"],
                "close_px": pr["close_px"], "winner": winner, "resolved_by": how, "market_id": pr["market_id"]})
            self._settle_positions(pr["coin"], pr["round"], winner, how)
        self.state["pending_rounds"] = keep

    def _settle_positions(self, coin: str, rnd: int, winner: str, how: str) -> None:
        keep = []
        for p in self.state["positions"]:
            if p["coin"] != coin or p["round"] != rnd:
                keep.append(p)
                continue
            payout = float(p["stake"]) if winner == "unresolved" else (float(p["qty"]) if p["side"] == winner else 0.0)
            pnl = payout - float(p["stake"])
            if p["book"] == "dip":
                self.state["dip_cash"] += payout
            else:
                self.state["cash"] += payout
            ms = now_ms()
            self._append_csv("bets.csv", BET_FIELDS, {**p, "settled": fmt_ms(ms, seconds=True), "settled_ms": ms, "winner": winner,
                                                      "payout": round(payout, 4), "pnl": round(pnl, 4),
                                                      "pnl_pct": round(pnl / float(p["stake"]) * 100, 2), "result": "won" if payout > 0 else "lost",
                                                      "resolved_by": how})
            self.log(f"SETTLED #{p['id']} {coin.upper()} {p['side']} ({p['kind']}): window ended {winner}, {pnl:+.2f} USD "
                     f"({pnl / float(p['stake']) * 100:+.1f}%). Bankroll {self.money(self.equity('fair'))}"
                     + (f", DipArb bankroll {self.money(self.equity('dip'))}" if p["book"] == "dip" else "") + ".")
        self.state["positions"] = keep
        self.save()

    # ---------- bookkeeping ----------
    def _equity_row(self, books: dict) -> None:
        ms = now_ms()
        self.state["last_eq_ms"] = ms
        self._append_csv("equity.csv", EQ_FIELDS, {"time": fmt_ms(ms, seconds=True), "ms": ms, "equity": round(self.equity("fair", books), 4),
                                                   "cash": round(self.state["cash"], 4), "dip_equity": round(self.equity("dip", books), 4),
                                                   "dip_cash": round(self.state["dip_cash"], 4)})

    def status_line(self, books: dict | None = None) -> str:
        bets = load_csv(self.jdir / "bets.csv")
        n = len(bets)
        won = int((bets["result"] == "won").sum()) if n else 0
        return (f"Fair-value bankroll {self.money(self.equity('fair', books))} ({n} settled, {won} won) | DipArb bankroll "
                f"{self.money(self.equity('dip', books))} ({self.state.get('dips', 0)} dips, {self.state.get('dips_hedged', 0)} hedged) | "
                f"open {len(self.state['positions'])} | ticks {self.state.get('ticks', 0)}")

    def _daily_report(self, books: dict) -> None:
        hour = int(self.cfg.get("ntfy", {}).get("daily_report_hour", 21))
        now = dt.datetime.now()
        day = now.strftime("%Y-%m-%d")
        if now.hour < hour or self.state.get("report_day") == day:
            return
        self.state["report_day"] = day
        self.notifier.send("Daily report: crypto 15-minute test [paper]", self.status_line(books), tags=["bar_chart"])
        self.save()


# ---------------- journal readers and research ----------------

def load_csv(p: Path):
    import pandas as pd
    p = Path(p)
    if p.exists() and p.stat().st_size > 0:
        return pd.read_csv(p, encoding="utf-8")
    return pd.DataFrame()


def research(jdir: Path, cfg: dict, log=print) -> dict:
    """Joins the ticks with the resolved windows and answers three questions: is the market price calibrated, is the
    model calibrated, and would buying when the model disagrees with the market have paid, after fee and slippage."""
    import pandas as pd
    jdir = Path(jdir)
    ticks, rounds = load_csv(jdir / "ticks.csv"), load_csv(jdir / "rounds.csv")
    out: dict = {}
    if ticks.empty or rounds.empty:
        log("Not enough data yet: the watcher needs ticks and at least one resolved window.")
        return out
    df = ticks.merge(rounds[["coin", "round", "winner", "resolved_by"]], on=["coin", "round"], how="inner")
    if df.empty:
        log("No ticks belong to a resolved window yet.")
        return out
    df["up_won"] = (df["winner"] == "Up").astype(float)
    df["up_mid"] = (df["up_bid"] + df["up_ask"]) / 2
    bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]
    labels = ["0-10", "10-20", "20-30", "30-40", "40-50", "50-60", "60-70", "70-80", "80-90", "90-100"]
    cal_m = df.assign(b=pd.cut(df["up_mid"], bins, labels=labels, right=False)).groupby("b", observed=True).agg(
        ticks=("up_won", "size"), windows=("round", "nunique"), avg_price=("up_mid", "mean"), up_won_pct=("up_won", lambda s: s.mean() * 100)).reset_index()
    cal_f = df.assign(b=pd.cut(df["fair_up"], bins, labels=labels, right=False)).groupby("b", observed=True).agg(
        ticks=("up_won", "size"), windows=("round", "nunique"), avg_model=("fair_up", "mean"), up_won_pct=("up_won", lambda s: s.mean() * 100)).reset_index()
    log(f"\n{len(df)} ticks in {df['round'].nunique()} resolved windows ({(df['resolved_by'] == 'binance_proxy').mean() * 100:.0f}% resolved "
        f"by the Binance proxy instead of Polymarket).")
    log("\nMarket calibration: UP mid price bucket (cents) -> how often the window actually ended UP")
    log(cal_m.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    log("\nModel calibration: model probability bucket -> how often the window actually ended UP")
    log(cal_f.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    rate = float(cfg.get("fee_rate", 0.07))
    tick = float(cfg.get("tick", 0.01))
    rows = []
    for thr in (0.0, 0.02, 0.04, 0.06, 0.10, 0.15):
        for side in ("Up", "Down"):
            ask = df["up_ask"] if side == "Up" else df["down_ask"]
            prob = df["fair_up"] if side == "Up" else 1 - df["fair_up"]
            fill = (ask + tick).clip(upper=0.99)
            cost = fill + rate * fill * (1 - fill)
            sel = df[(prob - cost >= thr) & (ask >= 0.03) & (ask <= 0.97) & (df["t_left"] >= 60)]
            if sel.empty:
                continue
            won = (sel["winner"] == side).astype(float)
            c = cost[sel.index]
            ret = (won / c - 1)
            rows.append({"min_edge": thr, "side": side, "ticks": len(sel), "windows": sel["round"].nunique(),
                         "won_pct": won.mean() * 100, "avg_return_pct": ret.mean() * 100})
    strat = pd.DataFrame(rows)
    log("\nWould buying have paid? Ticks where model - (ask + tick + fee) >= min_edge, held to resolution, return per dollar:")
    log(strat.to_string(index=False, float_format=lambda x: f"{x:.2f}") if not strat.empty else "  no signals yet")
    dips = load_csv(jdir / "dips.csv")
    if not dips.empty:
        log(f"\nDipArb replay: {len(dips)} dips, outcomes: " + ", ".join(f"{k} {v}" for k, v in dips["outcome"].value_counts().items())
            + f"; total P/L {pd.to_numeric(dips['pnl'], errors='coerce').sum():+.2f} USD on the closed ones; best pair price seen "
            f"{pd.to_numeric(dips['min_sum'], errors='coerce').min():.3f}.")
    out.update({"market": cal_m, "model": cal_f, "strategy": strat, "dips": dips})
    return out
