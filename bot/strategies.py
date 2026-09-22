"""The strategies. All are long-only: they buy and sell, never short (we trade spot).

Each strategy adds three columns to the data:
  entry  - True on the bar where we want to buy (the buy happens at the next bar's open)
  exit   - True on the bar where we want to sell
  atr    - volatility measure that sets where the stop goes
Stops and trailing stops are handled by the engine using stop_atr and trail_atr.
Every strategy can explain its signals in plain English (why_buy / why_sell).
"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind


class Strategy:
    name = "base"
    label = "Strategy"
    title = ""
    description = ""
    defaults: dict = {}

    def __init__(self, params: dict | None = None):
        p = {k: v for k, v in (params or {}).items() if k != "enabled"}
        self.p = {**self.defaults, **p}
        self.stop_atr = float(self.p.get("stop_atr", 2.0))
        self.trail_atr = float(self.p.get("trail_atr", 0.0))
        self.max_hold_bars = int(self.p.get("max_hold_bars", 0))
        self.atr_len = int(self.p.get("atr_len", 14))
        self.trend_ema = int(self.p.get("trend_ema", 0))

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["atr"] = ind.atr(out, self.atr_len).fillna(0.0)
        if self.trend_ema > 0:
            trend_ok = out["close"] > ind.ema(out["close"], self.trend_ema)
        else:
            trend_ok = pd.Series(True, index=out.index)
        self._signals(out)
        out["entry"] = (out["entry"].astype(bool) & trend_ok.astype(bool))
        out["exit"] = out["exit"].astype(bool)
        return out

    def _signals(self, out: pd.DataFrame) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return self.description.format(**self.p)

    def why_buy(self, tfw: str) -> str:
        """Plain English: why are we buying? tfw = the word for one bar, e.g. 'day'."""
        return "the strategy's entry condition is met"

    def why_sell(self, tfw: str) -> str:
        return "the strategy's exit condition is met"


class Breakout(Strategy):
    name = "breakout"
    label = "Breakout"
    title = "Breakout with volume"
    description = (
        "Buys when price closes above the highest high of the last {lookback} bars and volume is at least "
        "{vol_mult}x its average, provided price is above EMA{trend_ema}. Sells when price closes below the "
        "lowest low of the last {exit_lookback} bars. Stop {stop_atr} ATR, trailing stop {trail_atr} ATR."
    )
    defaults = dict(lookback=20, exit_lookback=10, vol_mult=1.5, vol_len=20, trend_ema=200,
                    atr_len=14, stop_atr=2.0, trail_atr=2.5, max_hold_bars=0)

    def _signals(self, out: pd.DataFrame) -> None:
        p = self.p
        hh = ind.donchian_high(out["high"], int(p["lookback"]))
        ll = ind.donchian_low(out["low"], int(p["exit_lookback"]))
        vol_avg = out["volume"].rolling(int(p["vol_len"])).mean().shift(1)
        out["entry"] = (out["close"] > hh) & (out["volume"] > float(p["vol_mult"]) * vol_avg)
        out["exit"] = out["close"] < ll

    def why_buy(self, tfw: str) -> str:
        s = f"price closed above its {int(self.p['lookback'])}-{tfw} high"
        if float(self.p.get("vol_mult", 0)) > 0:
            s += " on unusually high volume"
        if self.trend_ema > 0:
            s += f" and is above its {self.trend_ema}-{tfw} average"
        return s + ". Such a breakout tends to be followed by further gains"

    def why_sell(self, tfw: str) -> str:
        return f"price closed below its {int(self.p['exit_lookback'])}-{tfw} low, so the advance looks over"


class Trend(Strategy):
    name = "trend"
    label = "Trend"
    title = "Trend follower (EMA cross)"
    description = (
        "Buys when EMA{fast} crosses above EMA{slow} and price is above EMA{trend_ema}. "
        "Sells when EMA{fast} crosses below EMA{slow}. Stop {stop_atr} ATR, trailing stop {trail_atr} ATR."
    )
    defaults = dict(fast=20, slow=50, trend_ema=200, atr_len=14, stop_atr=2.5, trail_atr=3.0, max_hold_bars=0)

    def _signals(self, out: pd.DataFrame) -> None:
        ef = ind.ema(out["close"], int(self.p["fast"]))
        es = ind.ema(out["close"], int(self.p["slow"]))
        out["entry"] = ind.cross_up(ef, es)
        out["exit"] = ind.cross_down(ef, es)

    def why_buy(self, tfw: str) -> str:
        return (f"the short average ({int(self.p['fast'])} {tfw}s) crossed above the long average "
                f"({int(self.p['slow'])} {tfw}s), meaning the trend has turned up")

    def why_sell(self, tfw: str) -> str:
        return (f"the short average ({int(self.p['fast'])} {tfw}s) crossed below the long average "
                f"({int(self.p['slow'])} {tfw}s), meaning the trend has turned down")


class MeanRev(Strategy):
    name = "meanrev"
    label = "Mean reversion"
    title = "Mean reversion (Bollinger + RSI)"
    description = (
        "Buys dips in an uptrend: price closes below the lower Bollinger band ({bb_len}, {bb_std}) and RSI{rsi_len} "
        "is below {rsi_buy}, while price is above EMA{trend_ema}. Sells when price is back at the middle band or RSI "
        "is above {rsi_exit}, at the latest after {max_hold_bars} bars. Stop {stop_atr} ATR."
    )
    defaults = dict(bb_len=20, bb_std=2.0, rsi_len=14, rsi_buy=30, rsi_exit=55, trend_ema=200,
                    atr_len=14, stop_atr=2.0, trail_atr=0.0, max_hold_bars=24)

    def _signals(self, out: pd.DataFrame) -> None:
        p = self.p
        mid, _upper, lower = ind.bollinger(out["close"], int(p["bb_len"]), float(p["bb_std"]))
        r = ind.rsi(out["close"], int(p["rsi_len"]))
        out["entry"] = (out["close"] < lower) & (r < float(p["rsi_buy"]))
        out["exit"] = (out["close"] >= mid) | (r > float(p["rsi_exit"]))

    def why_buy(self, tfw: str) -> str:
        return (f"price dropped unusually far in a short time (below its lower Bollinger band, RSI under "
                f"{int(self.p['rsi_buy'])}) while the long-term trend still points up. Such dips tend to bounce")

    def why_sell(self, tfw: str) -> str:
        return f"price is back at its average (the middle band) or RSI is above {int(self.p['rsi_exit'])}. The bounce is done"


STRATEGIES = {"breakout": Breakout, "trend": Trend, "meanrev": MeanRev}


def load_strategies(cfg: dict) -> list[Strategy]:
    out: list[Strategy] = []
    for name, params in cfg.get("strategies", {}).items():
        if not params.get("enabled", True):
            continue
        cls = STRATEGIES.get(name)
        if cls is None:
            raise ValueError(f"Unknown strategy in config: '{name}'. Known: {', '.join(STRATEGIES)}")
        out.append(cls(params))
    if not out:
        raise ValueError("No strategy is enabled in the configuration")
    return out
