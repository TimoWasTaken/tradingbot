"""Technical indicators. Take pandas Series and return Series of the same length."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    """Exponential moving average. Reacts faster to new prices than a simple average."""
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI 0-100 (Wilder's method). Below 30 = oversold, above 70 = overbought."""
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(ad != 0.0, 100.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range: how much price typically moves per bar. Sets the size of stops."""
    prev = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    """Bollinger bands: the middle is a simple average, the bands sit k standard deviations away."""
    mid = sma(close, n)
    sd = close.rolling(n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def donchian_high(high: pd.Series, n: int) -> pd.Series:
    """Highest high of the previous n bars (excluding the current one)."""
    return high.rolling(n).max().shift(1)


def donchian_low(low: pd.Series, n: int) -> pd.Series:
    return low.rolling(n).min().shift(1)


def cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))
