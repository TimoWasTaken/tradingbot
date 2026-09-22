"""Copy-trading measurement: follow Polymarket's leaderboard wallets with paper money.

Public "Polymarket bot" repositories sell the idea of copying the top traders: take the leaderboard, keep the
wallets with a 60 %+ win rate, a profit factor of 1.5 or more, at least 30 closed positions, no single position
making up more than 30 % of the profit, and mirror their buys and sells. This module replays exactly that with
simulated money and, above all, measures the part those repositories never report: how late a home computer sees
the trade, how much worse the price is by then, and whether copying still pays after the taker fee.

What it does every minute:
- polls the recent trades of every followed wallet through Polymarket's public data API (no keys),
- for a new BUY it looks up the market and the live order book, and copies on paper at the best ask plus one tick
  and the taker fee, unless the market is about to end, is a 5- or 15-minute crypto window (over before we could
  act), or the price already moved more than the allowed premium; every signal is logged with the reason,
- for a SELL by the same wallet in the same market it closes the copied position at the best bid,
- otherwise positions ride to resolution.

Wallets are re-selected once a day from the leaderboards (overall and politics, month and week), scored on their
last closed positions. Nothing here sends an order."""
from __future__ import annotations

import csv
import datetime as dt
import json
import time
from pathlib import Path

import requests

from .config import fmt_ms, fmt_num, now_ms
from .polymarket import GAMMA, _fnum, _get, classify, fee_rate_for, fetch_books, normalize, resolution, taker_fee

DATA = "https://data-api.polymarket.com"

SIGNAL_FIELDS = ["time", "ms", "wallet", "name", "side", "slug", "question", "outcome", "their_price", "size", "usd", "delay_s",
                 "action", "reason", "our_price", "premium_pct"]
BET_FIELDS = ["id", "opened", "opened_ms", "wallet", "name", "slug", "question", "outcome", "outcome_index", "their_price", "price",
              "qty", "stake", "fee", "delay_s", "closed", "closed_ms", "exit", "exit_price", "payout", "pnl", "pnl_pct", "result"]
WALLET_FIELDS = ["day", "wallet", "name", "period_pnl", "closed_n", "win_rate", "profit_factor", "total_pnl", "max_single_share",
                 "updown_share", "last_trade_h", "passed", "reason"]
EQ_FIELDS = ["time", "ms", "equity", "cash", "open"]


# ---------------- data API ----------------

def _dget(path: str, params: dict, tries: int = 3):
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(f"{DATA}{path}", params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Polymarket data API failed: {path}: {last}")


def leaderboard(period: str = "MONTH", category: str = "OVERALL", limit: int = 50, offset: int = 0) -> list[dict]:
    raw = _dget("/v1/leaderboard", {"timePeriod": period, "orderBy": "PNL", "category": category, "limit": limit, "offset": offset})
    return [{"wallet": str(e.get("proxyWallet", "")).lower(), "name": str(e.get("userName") or e.get("proxyWallet", "")[:10]),
             "pnl": _fnum(e.get("pnl")), "volume": _fnum(e.get("vol") or e.get("volume")), "rank": e.get("rank"),
             "board": f"{category}/{period}"} for e in (raw if isinstance(raw, list) else [])]


def closed_positions(wallet: str, n: int = 100) -> list[dict]:
    out: list[dict] = []
    for offset in range(0, n, 50):
        raw = _dget("/closed-positions", {"user": wallet, "limit": 50, "offset": offset, "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
        if not raw:
            break
        out += raw
        if len(raw) < 50:
            break
        time.sleep(0.2)
    return out[:n]


def wallet_trades(wallet: str, limit: int = 30) -> list[dict]:
    raw = _dget("/trades", {"user": wallet, "limit": limit, "takerOnly": "false"})
    return raw if isinstance(raw, list) else []


def fetch_by_slug(slug: str) -> dict | None:
    raw = _get(f"{GAMMA}/markets", {"slug": slug})
    return normalize(raw[0]) if isinstance(raw, list) and raw else None


# ---------------- wallet selection ----------------

def judge_wallet(closed: list[dict], cfg: dict) -> dict:
    """The gate the public bots advertise, computed on the wallet's most recent closed positions."""
    g = cfg.get("gate", {})
    pnls = [_fnum(c.get("realizedPnl")) for c in closed]
    n = len(pnls)
    wins = [x for x in pnls if x > 0]
    losses = [-x for x in pnls if x < 0]
    total = sum(pnls)
    win_rate = len(wins) / n if n else 0.0
    pf = (sum(wins) / sum(losses)) if losses else (99.0 if wins else 0.0)
    max_single = (max(wins) / total) if wins and total > 0 else (1.0 if wins else 0.0)
    updown = sum(1 for c in closed if "updown" in str(c.get("slug") or "")) / n if n else 0.0
    reasons = []
    if n < int(g.get("min_closed", 30)):
        reasons.append(f"only {n} closed positions")
    if win_rate < float(g.get("min_win_rate", 0.6)):
        reasons.append(f"win rate {win_rate * 100:.0f}%")
    if pf < float(g.get("min_profit_factor", 1.5)):
        reasons.append(f"profit factor {pf:.2f}")
    if total < float(g.get("min_total_pnl", 500)):
        reasons.append(f"realised P/L {total:,.0f} USD")
    if max_single > float(g.get("max_single_share", 0.3)):
        reasons.append(f"one position is {max_single * 100:.0f}% of the profit")
    if updown > float(g.get("max_updown_share", 0.5)):
        reasons.append(f"{updown * 100:.0f}% of positions are 5/15-minute crypto windows (cannot be copied in time)")
    return {"closed_n": n, "win_rate": round(win_rate, 3), "profit_factor": round(min(pf, 99.0), 2), "total_pnl": round(total, 2),
            "max_single_share": round(max_single, 3), "updown_share": round(updown, 3), "passed": not reasons, "reason": "; ".join(reasons)}


def select_wallets(cfg: dict, log=print) -> tuple[list[dict], list[dict]]:
    """Returns (followed, all candidates judged)."""
    sel = cfg.get("selection", {})
    boards = sel.get("boards", [["OVERALL", "MONTH"], ["OVERALL", "WEEK"], ["POLITICS", "MONTH"]])
    per_board = int(sel.get("per_board", 40))
    cands: dict[str, dict] = {}
    for category, period in boards:
        try:
            for e in leaderboard(period, category, min(per_board, 50)):
                if e["wallet"] and e["wallet"] not in cands:
                    cands[e["wallet"]] = e
        except Exception as ex:  # noqa: BLE001
            log(f"leaderboard {category}/{period} failed: {ex}")
        time.sleep(0.3)
    log(f"{len(cands)} leaderboard wallets to judge ...")
    judged: list[dict] = []
    max_c = int(sel.get("max_candidates", 60))
    for i, (w, e) in enumerate(list(cands.items())[:max_c]):
        try:
            closed = closed_positions(w, int(sel.get("closed_lookback", 100)))
            j = judge_wallet(closed, cfg)
            trades = wallet_trades(w, 3)
            last_h = (time.time() - trades[0]["timestamp"]) / 3600.0 if trades else 9999.0
            if last_h > float(sel.get("max_idle_hours", 168)):
                j["passed"] = False
                j["reason"] = (j["reason"] + "; " if j["reason"] else "") + f"no trade for {last_h / 24:.0f} days"
            judged.append({**e, **j, "last_trade_h": round(last_h, 1)})
        except Exception as ex:  # noqa: BLE001
            log(f"could not judge {e['name']}: {ex}")
        if i % 10 == 9:
            log(f"  judged {i + 1} of {min(len(cands), max_c)} ...")
        time.sleep(0.3)
    passed = [j for j in judged if j["passed"]]
    passed.sort(key=lambda j: -j["pnl"])
    return passed[: int(sel.get("max_wallets", 15))], judged


# ---------------- the paper copier ----------------

class CopyTrader:
    def __init__(self, cfg: dict, jdir: Path, notifier, log):
        self.cfg = cfg
        self.jdir = Path(jdir)
        self.jdir.mkdir(parents=True, exist_ok=True)
        self.notifier = notifier
        self.log = log
        self.state_path = self.jdir / "state.json"
        self.state = self._load()
        self.sek_rate = float(cfg["capital"].get("sek_per_unit", 0) or 0)
        self.tick = float(cfg.get("tick", 0.01))
        self.market_cache: dict[str, tuple[float, dict | None]] = {}
        self.last_settle = 0.0

    def _load(self) -> dict:
        if self.state_path.exists() and self.state_path.stat().st_size > 0:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        start = float(self.cfg["capital"]["start"])
        return {"start": start, "cash": start, "positions": [], "bet_counter": 0, "wallets": {}, "wallets_day": "",
                "signals": 0, "copied": 0, "report_day": dt.datetime.now().strftime("%Y-%m-%d"), "last_eq_ms": 0,
                "since": fmt_ms(now_ms())}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

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

    def equity(self) -> float:
        return float(self.state["cash"]) + sum(float(p["qty"]) * float(p.get("mark", p["price"])) for p in self.state["positions"])

    # ---------- markets ----------
    def market(self, slug: str, max_age_s: float = 600) -> dict | None:
        got = self.market_cache.get(slug)
        if got and time.time() - got[0] < max_age_s:
            return got[1]
        m = None
        try:
            m = fetch_by_slug(slug)
        except Exception as e:  # noqa: BLE001
            self.log(f"market lookup failed for {slug}: {e}")
        self.market_cache[slug] = (time.time(), m)
        return m

    # ---------- wallets ----------
    def refresh_wallets(self, force: bool = False) -> None:
        day = dt.datetime.now().strftime("%Y-%m-%d")
        if not force and self.state.get("wallets_day") == day and self.state["wallets"]:
            return
        self.log("Selecting wallets from the leaderboards (this takes a minute) ...")
        followed, judged = select_wallets(self.cfg, self.log)
        for j in judged:
            self._append_csv("wallets.csv", WALLET_FIELDS, {"day": day, "wallet": j["wallet"], "name": j["name"], "period_pnl": round(j["pnl"]),
                                                            **{k: j.get(k, "") for k in WALLET_FIELDS if k not in ("day", "wallet", "name", "period_pnl")}})
        new_set = {j["wallet"] for j in followed}
        old = self.state["wallets"]
        added = [j for j in followed if j["wallet"] not in old]
        removed = [w for w in old if w not in new_set]
        keep: dict[str, dict] = {}
        for j in followed:
            prev = old.get(j["wallet"], {})
            keep[j["wallet"]] = {"name": j["name"], "board": j["board"], "pnl": j["pnl"], "win_rate": j["win_rate"],
                                 "profit_factor": j["profit_factor"], "closed_n": j["closed_n"], "added": prev.get("added", day),
                                 "seen": prev.get("seen", [])}
        # wallets with open copied positions stay tracked so their sells can still be mirrored
        for p in self.state["positions"]:
            if p["wallet"] not in keep and p["wallet"] in old:
                keep[p["wallet"]] = {**old[p["wallet"]], "retired": True}
        self.state["wallets"] = keep
        self.state["wallets_day"] = day
        self.log(f"Following {len(followed)} wallets ({len(judged)} judged, {len(added)} added, {len(removed)} dropped): "
                 + ", ".join(f"{j['name']} (WR {j['win_rate'] * 100:.0f}%, PF {j['profit_factor']:.1f}, {j['pnl'] / 1000:.0f}k)" for j in followed))
        self.save()

    # ---------- one cycle ----------
    def cycle(self) -> None:
        self.refresh_wallets()
        now = time.time()
        for w, info in list(self.state["wallets"].items()):
            try:
                trades = wallet_trades(w, int(self.cfg.get("trades_per_poll", 30)))
            except Exception as e:  # noqa: BLE001
                self.log(f"{info['name']}: trades fetch failed: {e}")
                continue
            seen = set(info.get("seen", []))
            keys = []
            fresh = []
            for t in trades:
                key = f"{t.get('transactionHash', '')[:18]}|{t.get('asset', '')[-10:]}|{t.get('side')}|{t.get('size')}"
                keys.append(key)
                if key not in seen:
                    fresh.append(t)
            learning = not seen                      # first look at a wallet: nothing is new, just remember
            info["seen"] = (list(seen) + keys)[-400:]
            if learning or info.get("retired") and not any(p["wallet"] == w for p in self.state["positions"]):
                continue
            for t in sorted(fresh, key=lambda x: x.get("timestamp", 0)):
                try:
                    self.handle_trade(w, info, t, now)
                except Exception as e:  # noqa: BLE001
                    self.log(f"{info['name']}: could not handle a trade: {e}")
            time.sleep(0.4)
        if now - self.last_settle >= float(self.cfg.get("settle_seconds", 600)):
            self.last_settle = now
            self.settle()
            self._equity_row()
        self._daily_report()
        self.save()

    # ---------- signals ----------
    def handle_trade(self, w: str, info: dict, t: dict, now: float) -> None:
        c = self.cfg.get("copy", {})
        side = str(t.get("side", "")).upper()
        price, size = _fnum(t.get("price")), _fnum(t.get("size"))
        usd = price * size
        delay = now - _fnum(t.get("timestamp"))
        slug = str(t.get("slug") or "")
        row = {"time": fmt_ms(now_ms(), seconds=True), "ms": now_ms(), "wallet": w, "name": info["name"], "side": side, "slug": slug,
               "question": str(t.get("title") or ""), "outcome": str(t.get("outcome") or ""), "their_price": price, "size": size,
               "usd": round(usd, 2), "delay_s": round(delay), "action": "", "reason": "", "our_price": "", "premium_pct": ""}
        self.state["signals"] = int(self.state.get("signals", 0)) + 1
        if side == "SELL":
            row["action"], row["reason"] = self.copy_sell(w, info, t, row)
        elif side == "BUY":
            row["action"], row["reason"] = self.copy_buy(w, info, t, row, delay, usd)
        else:
            row["action"], row["reason"] = "ignored", f"side {side}"
        self._append_csv("signals.csv", SIGNAL_FIELDS, row)

    def copy_buy(self, w: str, info: dict, t: dict, row: dict, delay: float, usd: float) -> tuple[str, str]:
        c = self.cfg.get("copy", {})
        slug = row["slug"]
        if info.get("retired"):
            return "skipped", "wallet no longer passes the gate"
        if delay > float(c.get("max_delay_s", 900)):
            return "skipped", f"seen {delay / 60:.0f} min late"
        if usd < float(c.get("min_signal_usd", 50)):
            return "skipped", f"their trade is only {usd:.0f} USD"
        if c.get("skip_updown", True) and "updown" in slug:
            return "skipped", "5/15-minute crypto window, over before a copy could fill"
        if any(p["wallet"] == w and p["asset"] == t.get("asset") for p in self.state["positions"]):
            return "skipped", "already copied this position"
        if len(self.state["positions"]) >= int(c.get("max_open", 12)):
            return "skipped", "max open copies reached"
        m = self.market(slug)
        if not m:
            return "skipped", "market not found"
        if m["closed"] or m["uma"] == "resolved":
            return "skipped", "market already closed"
        hours_left = (m["end"] - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600 if m["end"] else 9999
        if hours_left < float(c.get("min_hours_left", 1)):
            return "skipped", f"market ends in {hours_left * 60:.0f} min"
        asset = str(t.get("asset"))
        books = fetch_books([asset])
        b = books.get(asset)
        if not b or b["ask"] <= 0:
            return "skipped", "no ask in the book"
        their = row["their_price"]
        premium = b["ask"] / their - 1 if their > 0 else 0.0
        row["our_price"], row["premium_pct"] = b["ask"], round(premium * 100, 2)
        if premium > float(c.get("max_premium", 0.10)):
            return "skipped", f"price already {premium * 100:.0f}% above theirs"
        if b["ask"] < 0.02 or b["ask"] > 0.98:
            return "skipped", "price outside 2c-98c"
        fill = min(b["ask"] + self.tick, 0.99)
        rate = m["fee_rate"]
        cost_per_share = fill + rate * fill * (1 - fill)
        stake = min(self.equity() * float(c.get("stake_pct", 5)) / 100.0, float(self.state["cash"]))
        qty = min(stake / cost_per_share, b["ask_size"])
        if qty * cost_per_share < float(self.cfg.get("min_stake", 1.0)):
            return "skipped", "not enough cash or size"
        stake = qty * cost_per_share
        fee = taker_fee(fill, qty, rate)
        self.state["bet_counter"] += 1
        self.state["cash"] -= stake
        pos = {"id": self.state["bet_counter"], "opened": fmt_ms(now_ms(), seconds=True), "opened_ms": now_ms(), "wallet": w,
               "name": info["name"], "slug": slug, "question": m["question"], "asset": asset, "outcome": row["outcome"],
               "outcome_index": int(_fnum(t.get("outcomeIndex"), 0)), "their_price": their, "price": round(fill, 4), "qty": round(qty, 4),
               "stake": round(stake, 4), "fee": round(fee, 4), "delay_s": round(delay), "mark": round(fill, 4), "end_date": m["end"].strftime("%Y-%m-%d") if m["end"] else ""}
        self.state["positions"].append(pos)
        self.state["copied"] = int(self.state.get("copied", 0)) + 1
        self.log(f"COPY #{pos['id']}: {info['name']} bought {row['outcome']} on \"{m['question'][:60]}\" at {their:.2f} "
                 f"({usd:,.0f} USD) {delay / 60:.1f} min ago; we buy {qty:.1f} shares at {fill:.2f} ({self.money(stake)}, fee {fee:.3f}).")
        if self.cfg.get("ntfy", {}).get("notify_each_copy", False):
            self.notifier.send(f"Copy-trade #{pos['id']} [paper]", f"{info['name']}: {row['outcome']} on {m['question']} at {fill:.2f}, "
                               f"{self.money(stake)}, seen {delay / 60:.0f} min late.", tags=["busts_in_silhouette"])
        return "copied", ""

    def copy_sell(self, w: str, info: dict, t: dict, row: dict) -> tuple[str, str]:
        mine = [p for p in self.state["positions"] if p["wallet"] == w and p["asset"] == str(t.get("asset"))]
        if not mine:
            return "ignored", "we hold nothing from this signal"
        asset = str(t.get("asset"))
        books = fetch_books([asset])
        b = books.get(asset)
        if not b or b["bid"] <= 0:
            return "skipped", "no bid to sell into"
        row["our_price"] = b["bid"]
        for p in mine:
            m = self.market(p["slug"])
            rate = m["fee_rate"] if m else 0.0
            fill = max(b["bid"] - self.tick, 0.01)
            proceeds = float(p["qty"]) * fill - taker_fee(fill, float(p["qty"]), rate)
            self._close(p, "exit_copied", fill, proceeds)
        return "sold", ""

    def _close(self, p: dict, how: str, exit_price: float, payout: float) -> None:
        self.state["cash"] += payout
        pnl = payout - float(p["stake"])
        ms = now_ms()
        self._append_csv("bets.csv", BET_FIELDS, {**p, "closed": fmt_ms(ms, seconds=True), "closed_ms": ms, "exit": how,
                                                  "exit_price": round(exit_price, 4), "payout": round(payout, 4), "pnl": round(pnl, 4),
                                                  "pnl_pct": round(pnl / float(p["stake"]) * 100, 2), "result": "won" if pnl > 0 else "lost"})
        self.state["positions"] = [x for x in self.state["positions"] if x is not p]
        self.log(f"CLOSED #{p['id']} ({how}): {p['outcome']} on \"{p['question'][:60]}\" bought {p['price']:.2f}, out {exit_price:.2f}, "
                 f"{pnl:+.2f} USD ({pnl / float(p['stake']) * 100:+.1f}%). Bankroll {self.money(self.equity())}.")

    # ---------- settlement and marks ----------
    def settle(self) -> None:
        for p in list(self.state["positions"]):
            m = self.market(p["slug"], max_age_s=60)
            if not m:
                continue
            win = resolution(m)
            if win is not None:
                payout = float(p["qty"]) if win == int(p["outcome_index"]) else 0.0
                self._close(p, "resolved", 1.0 if payout else 0.0, payout)
                continue
            if m["prices"] and len(m["prices"]) == 2:
                p["mark"] = float(m["prices"][int(p["outcome_index"])])
            elif m["end"] and (dt.datetime.now(dt.timezone.utc) - m["end"]).days >= int(self.cfg.get("refund_after_days", 14)):
                self._close(p, "unresolved_refund", float(p["price"]), float(p["stake"]))

    def _equity_row(self) -> None:
        ms = now_ms()
        self.state["last_eq_ms"] = ms
        self._append_csv("equity.csv", EQ_FIELDS, {"time": fmt_ms(ms, seconds=True), "ms": ms, "equity": round(self.equity(), 4),
                                                   "cash": round(self.state["cash"], 4), "open": len(self.state["positions"])})

    def status_line(self) -> str:
        bets = load_csv(self.jdir / "bets.csv")
        n = len(bets)
        won = int((bets["result"] == "won").sum()) if n else 0
        return (f"Bankroll {self.money(self.equity())} | {len(self.state['positions'])} open copies | {n} closed ({won} won) | "
                f"{self.state.get('copied', 0)} copied of {self.state.get('signals', 0)} signals | following {len(self.state['wallets'])} wallets")

    def _daily_report(self) -> None:
        hour = int(self.cfg.get("ntfy", {}).get("daily_report_hour", 21))
        now = dt.datetime.now()
        day = now.strftime("%Y-%m-%d")
        if now.hour < hour or self.state.get("report_day") == day:
            return
        self.state["report_day"] = day
        self.notifier.send("Daily report: copy-trading test [paper]", self.status_line(), tags=["bar_chart"])


def load_csv(p: Path):
    import pandas as pd
    p = Path(p)
    if p.exists() and p.stat().st_size > 0:
        return pd.read_csv(p, encoding="utf-8")
    return pd.DataFrame()
