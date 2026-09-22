"""Fund rotation (momentum across broad asset classes), one decision per month.

The rule:
  1. On the last trading day of the month, score each asset by the average of its 3-, 6- and 12-month returns.
  2. Buy the top N with equal weight, but only if they beat the safe asset (short-term bonds).
     Slots that do not beat bonds go to the bond fund instead.
  3. The switch is executed at the next trading day's close (funds trade at daily NAV).
History is measured on US ETFs (long history). In the notifications they are mapped to commission-free
Avanza funds, so a switch costs nothing beyond the funds' own yearly fees."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

from . import data_stocks
from .config import fmt_ms, fmt_num

US_BDAY = CustomBusinessDay(calendar=USFederalHolidayCalendar())
MARKET = data_stocks.get_market("stocks_us")


# ---------------- data ----------------

def build_close(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """{ticker: candle frame} -> table of closes, rows = trading days (open_time ms)."""
    cols = {}
    for t, df in frames.items():
        if df is None or df.empty:
            continue
        cols[t] = pd.Series(df["close"].to_numpy(dtype=float), index=df["open_time"].to_numpy(dtype=np.int64))
    close = pd.DataFrame(cols).sort_index()
    close = close.ffill()
    first_full = close.dropna().index.min()
    return close.loc[first_full:].dropna()


def load_close_history(cfg: dict, days: int, log=print) -> pd.DataFrame:
    tickers = list(cfg["universe"]) + [cfg["safe"]] + list(cfg.get("benchmarks", {}))
    frames = data_stocks.load_history(list(dict.fromkeys(tickers)), days, log=log, market=MARKET)
    return build_close(frames)


def fetch_close_recent(cfg: dict) -> pd.DataFrame:
    """Closed days over the last two years (today's unfinished bar excluded)."""
    tickers = list(dict.fromkeys(list(cfg["universe"]) + [cfg["safe"]]))
    recent = data_stocks.fetch_recent(tickers, period="2y", market=MARKET)
    return build_close({t: closed for t, (closed, _cur) in recent.items()})


def ms_to_date(ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(int(ms) / 1000, tz=MARKET.tz).date()


def is_month_end(ms: int) -> bool:
    """Is this trading day the last of its month? (the next US business day falls in another month)"""
    d = pd.Timestamp(ms_to_date(ms))
    return (d + US_BDAY).month != d.month


# ---------------- the rule ----------------

def scores(close: pd.DataFrame, i: int, lookbacks: list[int]) -> pd.Series:
    """Momentum per column on row i: the average return over the lookbacks (trading days)."""
    row = close.iloc[i]
    parts = []
    for lb in lookbacks:
        if i - lb < 0:
            return pd.Series(np.nan, index=close.columns)
        parts.append(row / close.iloc[i - lb] - 1.0)
    return sum(parts) / len(parts)


def decide(close: pd.DataFrame, i: int, cfg: dict) -> tuple[list[str], pd.Series]:
    """Returns (assets to hold, one per slot; may contain the safe asset several times, scores)."""
    lookbacks = [int(x) for x in cfg.get("lookbacks", [63, 126, 252])]
    sc = scores(close, i, lookbacks)
    if sc.isna().any():
        return [cfg["safe"]] * int(cfg["top_n"]), sc
    universe = list(cfg["universe"])
    safe = cfg["safe"]
    ranked = sc[universe].sort_values(ascending=False)
    trend_len = int(cfg.get("trend_sma", 0) or 0)
    chosen: list[str] = []
    for t in ranked.index[: int(cfg["top_n"])]:
        ok = sc[t] > sc[safe]
        if ok and trend_len > 0 and i >= trend_len:
            ok = close[t].iloc[i] > close[t].iloc[i - trend_len + 1: i + 1].mean()
        chosen.append(t if ok else safe)
    return chosen, sc


def target_weights(chosen: list[str]) -> dict[str, float]:
    w: dict[str, float] = {}
    for t in chosen:
        w[t] = w.get(t, 0.0) + 1.0 / len(chosen)
    return w


# ---------------- backtest ----------------

def run_backtest(cfg: dict, close: pd.DataFrame, label: str = "") -> dict:
    start_cap = float(cfg["capital"]["start"])
    fee_year = float(cfg.get("fund_fee_pct_per_year", 0.3)) / 100.0
    slip = float(cfg.get("slippage_pct", 0.1)) / 100.0
    warmup = max(int(x) for x in cfg.get("lookbacks", [63, 126, 252]))
    warmup = max(warmup, int(cfg.get("trend_sma", 0) or 0))
    idx = close.index.to_numpy()
    units: dict[str, float] = {}
    cash = start_cap
    pending: dict[str, float] | None = None
    equity = []
    alloc_hist = []      # (month-end ms, list of assets)
    switches = []        # (execution ms, from, to, value)
    current: list[str] = []
    started = False
    for i in range(len(close)):
        ms = int(idx[i])
        row = close.iloc[i]
        if pending is not None:
            value = cash + sum(u * row[t] for t, u in units.items())
            value *= (1 - slip)
            units = {t: value * w / row[t] for t, w in pending.items()}
            cash = 0.0
            pending = None
        if units:
            daily_fee = fee_year / 252
            units = {t: u * (1 - daily_fee) for t, u in units.items()}
        value = cash + sum(u * row[t] for t, u in units.items())
        if started:
            equity.append((ms, value))
        if i >= warmup and is_month_end(ms) and i + 1 < len(close):
            chosen, _sc = decide(close, i, cfg)
            if not started:
                started = True
                equity.append((ms, value))
            if sorted(chosen) != sorted(current):
                switches.append({"ms": int(idx[i + 1]), "from": list(current), "to": list(chosen), "value": value})
                current = list(chosen)
                pending = target_weights(chosen)
            alloc_hist.append((ms, list(chosen)))
    eq = pd.Series({ms: v for ms, v in equity}).sort_index()
    return {"label": label, "equity": eq, "switches": switches, "alloc": alloc_hist, "start_cap": start_cap}


def stats_from_equity(eq: pd.Series, start_cap: float) -> dict:
    if len(eq) < 2:
        return {"total_pct": 0.0, "cagr_pct": 0.0, "max_dd_pct": 0.0, "worst_year_pct": 0.0, "best_year_pct": 0.0,
                "years": 0.0, "per_year": {}}
    years = (eq.index[-1] - eq.index[0]) / 1000 / 86400 / 365.25
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0
    dd = (eq / eq.cummax() - 1).min()
    dates = pd.to_datetime(eq.index, unit="ms")
    yearly = eq.groupby(dates.year).agg(["first", "last"])
    yr = (yearly["last"] / yearly["first"] - 1) * 100
    return {"total_pct": total * 100, "cagr_pct": cagr * 100, "max_dd_pct": dd * 100,
            "worst_year_pct": float(yr.min()), "best_year_pct": float(yr.max()), "years": years,
            "per_year": {int(k): float(v) for k, v in yr.items()}}


def benchmark_equity(close: pd.DataFrame, ticker: str, eq: pd.Series, start_cap: float) -> pd.Series:
    c = close[ticker].reindex(eq.index).ffill()
    return c / c.iloc[0] * start_cap


# ---------------- paper trading (live) ----------------

class RotationPaper:
    def __init__(self, cfg: dict, jdir: Path, notifier, log):
        self.cfg = cfg
        self.jdir = Path(jdir)
        self.jdir.mkdir(parents=True, exist_ok=True)
        self.notifier = notifier
        self.log = log
        self.state_path = self.jdir / "state.json"
        self.state = self._load()
        self.fund_names = dict(cfg.get("fund_names", {}))
        self.asset_names = dict(cfg.get("ticker_names", {}))

    def _load(self) -> dict:
        if self.state_path.exists() and self.state_path.stat().st_size > 0:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"start": float(self.cfg["capital"]["start"]), "cash": float(self.cfg["capital"]["start"]),
                "units": {}, "current": [], "pending": None, "last_decision_ms": 0, "last_valued_ms": 0,
                "switches": 0}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def asset(self, ticker: str) -> str:
        return self.asset_names.get(ticker, ticker)

    def fund(self, ticker: str) -> str:
        return self.fund_names.get(ticker, ticker)

    def both(self, ticker: str) -> str:
        return f"{self.asset(ticker)} ({self.fund(ticker)})"

    def value(self, row: pd.Series) -> float:
        return float(self.state["cash"]) + sum(float(u) * float(row[t]) for t, u in self.state["units"].items())

    def _append_csv(self, name: str, header: str, line: str) -> None:
        p = self.jdir / name
        new = not p.exists() or p.stat().st_size == 0
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write(header + "\n")
            f.write(line + "\n")

    def cycle(self) -> None:
        cfg = self.cfg
        close = fetch_close_recent(cfg)
        if close.empty:
            self.log("No price data, will retry later.")
            return
        i = len(close) - 1
        ms = int(close.index[i])
        row = close.iloc[i]
        st = self.state
        # 1. execute a pending switch at the latest close
        if st.get("pending") and ms > int(st["pending"]["decided_ms"]):
            slip = float(cfg.get("slippage_pct", 0.1)) / 100.0
            value = self.value(row) * (1 - slip)
            weights = st["pending"]["weights"]
            st["units"] = {t: value * w / float(row[t]) for t, w in weights.items()}
            st["cash"] = 0.0
            st["current"] = list(st["pending"]["chosen"])
            st["switches"] = int(st.get("switches", 0)) + 1
            st["pending"] = None
            txt = ", ".join(f"{w * 100:.0f}% {self.both(t)}" for t, w in weights.items())
            self.log(f"[{fmt_ms(ms)}] SWITCH EXECUTED (paper): portfolio is now {txt}. Value {fmt_num(value, 0)} SEK.")
            self._append_csv("switches.csv", "time,ms,portfolio,value",
                             f"{fmt_ms(ms)},{ms},{' + '.join(self.asset(t) for t in st['current'])},{value:.2f}")
            self.notifier.send_both("Fund rotation: switch executed [paper]",
                                    f"Portfolio is now {txt}.\nPaper value {fmt_num(value, 0)} SEK "
                                    f"({(value / st['start'] - 1) * 100:+.1f}% since start).",
                                    tags=["arrows_counterclockwise"])
        # 2. daily valuation
        if ms > int(st.get("last_valued_ms", 0)):
            value = self.value(row)
            self._append_csv("equity.csv", "time,ms,equity", f"{fmt_ms(ms)},{ms},{value:.2f}")
            st["last_valued_ms"] = ms
            self.log(f"[{fmt_ms(ms)}] Value {fmt_num(value, 0)} SEK ({(value / st['start'] - 1) * 100:+.1f}% since start) | "
                     f"holdings: {', '.join(self.asset(t) for t in st['current']) or 'none yet'}")
        # 3. month-end decision (or the starting decision the very first time)
        first_time = int(st.get("last_decision_ms", 0)) == 0
        if (first_time or is_month_end(ms)) and ms > int(st.get("last_decision_ms", 0)) and not st.get("pending"):
            chosen, sc = decide(close, i, cfg)
            st["last_decision_ms"] = ms
            if sorted(chosen) != sorted(st["current"]):
                st["pending"] = {"chosen": chosen, "weights": target_weights(chosen), "decided_ms": ms}
            self.announce(chosen, sc, ms, first_time)
        self.save()

    def announce(self, chosen: list[str], sc: pd.Series, ms: int, first_time: bool) -> None:
        cfg = self.cfg
        st = self.state
        safe = cfg["safe"]
        ranking = sc[list(cfg["universe"]) + [safe]].sort_values(ascending=False)
        rank_txt = "\n".join(f"  {self.asset(t)}: {v * 100:+.1f}%" for t, v in ranking.items())
        target = target_weights(chosen)
        hold_txt = ", ".join(f"{w * 100:.0f}% {self.both(t)}" for t, w in target.items())
        cur = set(st["current"])
        new = set(chosen)
        sell = [self.both(t) for t in cur - new]
        buy = [self.both(t) for t in new - cur]
        if first_time:
            title = "Fund rotation: starting portfolio"
            what = f"The bot starts with {hold_txt}."
        elif not sell and not buy:
            title = "Fund rotation: no changes this month"
            what = f"Keep {hold_txt}."
        else:
            title = "Fund rotation: month-end switch"
            what = f"Sell {', '.join(sell) or 'nothing'}. Buy {', '.join(buy) or 'nothing'}. New portfolio: {hold_txt}."
        n_safe = chosen.count(safe)
        why = (f"The {int(cfg['top_n'])} of {len(cfg['universe'])} broad asset classes with the best average 3/6/12-month "
               f"return are held. Any slot that does not beat short-term bonds goes to bonds instead"
               + (f" ({n_safe} of {len(chosen)} slots right now)." if n_safe else "."))
        msg = (f"{what}\nWhy: {why}\nRanking (average 3/6/12-month return):\n{rank_txt}\n"
               f"To follow this on Avanza: place the fund orders before 13:00 on the next weekday so they trade at that "
               f"day's price. Fund switches are commission-free there. Fund names in brackets are suggestions.")
        self.log(f"[{fmt_ms(ms)}] {title}: " + msg.replace("\n", " "))
        self.notifier.send_both(title + " [paper]", msg, tags=["calendar"])
