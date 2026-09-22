"""Journal: every closed trade and the equity curve are written to CSV.
All statistics in the reports are computed from here, the way a professional keeps a trading journal."""
from __future__ import annotations

import csv
import math
from pathlib import Path

import pandas as pd

TRADE_FIELDS = ["id", "symbol", "strategy", "entry_time", "entry_ms", "entry_price", "qty", "cost",
                "exit_time", "exit_ms", "exit_price", "proceeds", "pnl", "pnl_pct", "fees", "reason", "bars"]
EQUITY_FIELDS = ["time", "ms", "equity", "cash"]


class Journal:
    """Writes to disk immediately so nothing is lost if the bot crashes."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trades_path = self.dir / "trades.csv"
        self.equity_path = self.dir / "equity.csv"

    def append_trade(self, trade: dict) -> None:
        self._append(self.trades_path, TRADE_FIELDS, trade)

    def append_equity(self, row: dict) -> None:
        self._append(self.equity_path, EQUITY_FIELDS, row)

    @staticmethod
    def _append(path: Path, fields, row: dict) -> None:
        new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def load_trades(self) -> pd.DataFrame:
        if self.trades_path.exists() and self.trades_path.stat().st_size > 0:
            return pd.read_csv(self.trades_path, encoding="utf-8")
        return pd.DataFrame(columns=TRADE_FIELDS)

    def load_equity(self) -> pd.DataFrame:
        if self.equity_path.exists() and self.equity_path.stat().st_size > 0:
            return pd.read_csv(self.equity_path, encoding="utf-8")
        return pd.DataFrame(columns=EQUITY_FIELDS)


class MemoryJournal(Journal):
    """Used by the backtest: everything stays in memory, nothing is written to disk."""

    def __init__(self):  # noqa: D107 - no directory needed
        self.trades: list[dict] = []
        self.equity: list[dict] = []

    def append_trade(self, trade: dict) -> None:
        self.trades.append(trade)

    def append_equity(self, row: dict) -> None:
        self.equity.append(row)

    def load_trades(self) -> pd.DataFrame:
        return pd.DataFrame(self.trades, columns=TRADE_FIELDS)

    def load_equity(self) -> pd.DataFrame:
        return pd.DataFrame(self.equity, columns=EQUITY_FIELDS)


# ---------------- statistics ----------------

def max_drawdown_pct(equity: pd.Series) -> float:
    if equity is None or len(equity) == 0:
        return 0.0
    eq = equity.astype(float)
    peak = eq.cummax()
    dd = (eq / peak - 1.0) * 100.0
    return float(dd.min())


def losing_streak(pnl) -> int:
    best = cur = 0
    for v in pnl:
        cur = cur + 1 if v <= 0 else 0
        best = max(best, cur)
    return best


def _group(trades: pd.DataFrame, col: str) -> list[dict]:
    rows = []
    for name, g in trades.groupby(col, sort=True):
        pnl = g["pnl"].astype(float)
        pct = g["pnl_pct"].astype(float)
        w = pnl > 0
        rows.append({
            "name": str(name), "count": int(len(g)), "win_rate_pct": float(w.mean() * 100),
            "pnl": float(pnl.sum()), "expectancy_pct": float(pct.mean()),
            "avg_win_pct": float(pct[w].mean()) if w.any() else 0.0,
            "avg_loss_pct": float(pct[~w].mean()) if (~w).any() else 0.0,
        })
    return rows


def compute_stats(trades: pd.DataFrame, equity: pd.DataFrame, start_equity: float) -> dict:
    s: dict = {"count": int(len(trades)), "start_equity": float(start_equity)}
    eq = equity["equity"].astype(float) if len(equity) else pd.Series(dtype=float)
    if len(eq):
        end = float(eq.iloc[-1])
    else:
        end = float(start_equity) + (float(trades["pnl"].sum()) if len(trades) else 0.0)
    s["end_equity"] = end
    s["return_pct"] = (end / start_equity - 1.0) * 100.0 if start_equity else 0.0
    s["max_dd_pct"] = max_drawdown_pct(eq)
    if len(trades) == 0:
        for k in ("wins", "losses", "win_rate_pct", "avg_win_pct", "avg_loss_pct", "payoff",
                  "profit_factor", "expectancy", "expectancy_pct", "total_pnl", "fees",
                  "pnl_before_fees", "return_before_fees_pct",
                  "best_pct", "worst_pct", "avg_bars", "losing_streak", "stop_share_pct"):
            s[k] = 0.0
        s["per_strategy"] = []
        s["per_symbol"] = []
        return s
    pnl = trades["pnl"].astype(float)
    pct = trades["pnl_pct"].astype(float)
    w = pnl > 0
    s["wins"] = int(w.sum())
    s["losses"] = int((~w).sum())
    s["win_rate_pct"] = float(w.mean() * 100)
    s["avg_win_pct"] = float(pct[w].mean()) if w.any() else 0.0
    s["avg_loss_pct"] = float(pct[~w].mean()) if (~w).any() else 0.0
    s["payoff"] = s["avg_win_pct"] / abs(s["avg_loss_pct"]) if s["avg_loss_pct"] else math.inf
    gross_win = float(pnl[w].sum())
    gross_loss = abs(float(pnl[~w].sum()))
    s["profit_factor"] = gross_win / gross_loss if gross_loss else math.inf
    s["expectancy"] = float(pnl.mean())
    s["expectancy_pct"] = float(pct.mean())
    s["total_pnl"] = float(pnl.sum())
    s["fees"] = float(trades["fees"].astype(float).sum())
    s["pnl_before_fees"] = s["total_pnl"] + s["fees"]
    s["return_before_fees_pct"] = s["pnl_before_fees"] / start_equity * 100.0 if start_equity else 0.0
    s["best_pct"] = float(pct.max())
    s["worst_pct"] = float(pct.min())
    s["avg_bars"] = float(trades["bars"].astype(float).mean())
    s["losing_streak"] = losing_streak(pnl.tolist())
    s["stop_share_pct"] = float((trades["reason"].astype(str) == "stop").mean() * 100)
    s["per_strategy"] = _group(trades, "strategy")
    s["per_symbol"] = _group(trades, "symbol")
    return s
