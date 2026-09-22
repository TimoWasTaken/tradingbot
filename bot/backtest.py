"""Backtest: runs the strategies on historical data with exactly the same core as the live bot.

Rules to avoid fooling ourselves:
  - a signal on a CLOSED bar -> the trade happens at the NEXT bar's open
  - every buy and sell is charged fees and slippage
  - stops are checked against each bar's low (a gap below the stop fills at the open)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .broker import PaperBroker
from .config import fmt_ms
from .engine import Trader
from .journal import MemoryJournal, compute_stats
from .risk import RiskManager


@dataclass
class BacktestResult:
    label: str
    trades: pd.DataFrame
    equity: pd.DataFrame
    stats: dict
    buyhold: dict
    start_ms: int
    end_ms: int


def make_paper_broker(cfg: dict) -> PaperBroker:
    r = cfg["risk"]
    return PaperBroker(float(r.get("fee_pct", 0.1)), float(r.get("slippage_pct", 0.05)),
                       min_fee=float(r.get("min_fee", 0.0)), whole_shares=bool(r.get("whole_shares", False)))


def run_backtest(cfg: dict, data: dict[str, pd.DataFrame], strategies: list, label: str = "",
                 kill_switch: bool = False, benchmark: pd.DataFrame | None = None,
                 benchmark_name: str = "") -> BacktestResult:
    """kill_switch=False: the kill switch does not stop the test, but the date it would have fired is recorded.
    benchmark: an index to compare against (e.g. OMXS30). Without one, buy & hold of each symbol is used."""
    start_cap = float(cfg["capital"]["start"])
    risk_cfg = dict(cfg["risk"])
    orig_dd = float(risk_cfg.get("max_drawdown_pct", 25.0))
    if not kill_switch:
        risk_cfg["max_drawdown_pct"] = 1e9
    risk = RiskManager(risk_cfg, start_cap)
    breach_ts: int | None = None
    broker = make_paper_broker(cfg)
    journal = MemoryJournal()
    trader = Trader(cfg, risk, broker, journal, notifier=None, log=lambda *_a, **_k: None)

    prepared = []
    all_ts: set[int] = set()
    for symbol, df in data.items():
        if df is None or len(df) == 0:
            continue
        for strat in strategies:
            sig = strat.prepare(df)
            arr = {c: sig[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "atr")}
            arr["ts"] = sig["open_time"].to_numpy(dtype=np.int64)
            arr["entry"] = sig["entry"].to_numpy(dtype=bool)
            arr["exit"] = sig["exit"].to_numpy(dtype=bool)
            index = {int(t): i for i, t in enumerate(arr["ts"])}
            prepared.append(((symbol, strat.name), strat, arr, index))
            all_ts.update(index.keys())
    closes = {s: {int(t): float(c) for t, c in zip(df["open_time"], df["close"])}
              for s, df in data.items() if df is not None and len(df)}
    timeline = sorted(all_ts)
    if not timeline:
        raise RuntimeError("No data to test on.")
    record_every = max(1, len(timeline) // 3000)

    for n, ts in enumerate(timeline):
        risk.new_time(ts, trader.equity())
        for key, strat, arr, index in prepared:
            i = index.get(ts)
            if i is None or i + 1 >= len(arr["ts"]):
                continue
            symbol = key[0]
            pos = trader.positions.get(key)
            if pos is not None and arr["low"][i] <= pos.stop:
                ref = min(float(arr["open"][i]), pos.stop)
                fill = broker.sell(symbol, pos.qty, ref, ts)
                trader.apply_sell(key, fill, "stop")
            bar = {"close": arr["close"][i], "entry": arr["entry"][i], "exit": arr["exit"][i], "atr": arr["atr"][i]}
            intents = trader.evaluate_bar(key, bar, strat)
            if not intents:
                continue
            nxt_open = float(arr["open"][i + 1])
            nxt_ts = int(arr["ts"][i + 1])
            for it in intents:
                if it.kind == "sell":
                    pos = trader.positions.get(key)
                    if pos is None:
                        continue
                    fill = broker.sell(symbol, pos.qty, nxt_open, nxt_ts)
                    trader.apply_sell(key, fill, it.reason)
                else:
                    fill = broker.buy(symbol, it.quote, nxt_open, nxt_ts)
                    if fill is None:
                        trader.stats["too_little_capital"] += 1
                        continue
                    trader.apply_buy(it, fill, strat)
        prices = {s: m[ts] for s, m in closes.items() if ts in m}
        trader.mark(ts, prices, record=(n % record_every == 0))
        if breach_ts is None and risk.max_dd <= -orig_dd:
            breach_ts = int(ts)
        if risk.killed and trader.positions:
            trader.close_all(ts, prices, "kill_switch")

    last_ts = timeline[-1]
    prices = {s: m[last_ts] for s, m in closes.items() if last_ts in m}
    if trader.positions:
        trader.close_all(last_ts, prices, "end_of_data")
    trader.mark(last_ts, prices, record=True)

    trades = journal.load_trades()
    equity = journal.load_equity()
    buyhold: dict[str, float] = {}
    if benchmark is not None and len(benchmark):
        b = benchmark[(benchmark["open_time"] >= timeline[0]) & (benchmark["open_time"] <= last_ts)]
        if len(b):
            bclose = {int(t): float(c) for t, c in zip(b["open_time"], b["close"])}
            first = float(b["close"].iloc[0])
            name = benchmark_name or "index"
            equity[f"bh_{name}"] = [bclose.get(int(ms), np.nan) / first * start_cap for ms in equity["ms"]]
            buyhold[name] = (float(b["close"].iloc[-1]) / first - 1) * 100
    else:
        for s, df in data.items():
            if df is None or len(df) == 0:
                continue
            first = float(df["close"].iloc[0])
            equity[f"bh_{s}"] = [closes[s].get(int(ms), np.nan) / first * start_cap for ms in equity["ms"]]
            buyhold[s] = (float(df["close"].iloc[-1]) / first - 1) * 100
    stats = compute_stats(trades, equity, start_cap)
    stats["max_dd_pct"] = min(stats["max_dd_pct"], risk.max_dd)
    stats["daily_halts"] = risk.daily_halts
    stats["kill_switch"] = bool(risk.killed)
    stats["kill_switch_date"] = fmt_ms(breach_ts) if breach_ts is not None else ""
    stats["kill_switch_limit"] = orig_dd
    stats["blocked_signals"] = trader.stats["blocked_signals"]
    stats["too_little_capital"] = trader.stats["too_little_capital"]
    return BacktestResult(label, trades, equity, stats, buyhold, int(timeline[0]), int(last_ts))
