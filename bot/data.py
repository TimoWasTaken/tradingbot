"""Candle data from Binance's public API. No API key needed. History is cached locally under data/."""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

PUBLIC_URLS = ["https://api.binance.com", "https://data-api.binance.vision"]
TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "12h": 43_200_000, "1d": 86_400_000,
}
_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
         "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
KEEP = ["open_time", "open", "high", "low", "close", "volume", "close_time"]


def timeframe_ms(tf: str) -> int:
    if tf not in TF_MS:
        raise ValueError(f"Unknown timeframe '{tf}'. Choose one of: {', '.join(TF_MS)}")
    return TF_MS[tf]


def _get_json(path: str, params: dict, tries: int = 3):
    last: Exception | None = None
    for _ in range(tries):
        for base in PUBLIC_URLS:
            try:
                r = requests.get(base + path, params=params, timeout=20)
                if r.status_code in (418, 429):
                    last = RuntimeError(f"Binance is rate-limiting requests (HTTP {r.status_code})")
                    time.sleep(10)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                last = e
        time.sleep(2)
    raise RuntimeError(f"Could not fetch data from Binance: {last}")


def _to_df(raw) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=KEEP)
    df = pd.DataFrame(raw, columns=_COLS)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["close_time"].astype("int64")
    return df[KEEP]


def fetch_klines(symbol: str, timeframe: str, start_ms: int | None = None,
                 end_ms: int | None = None, limit: int = 1000) -> pd.DataFrame:
    params: dict = {"symbol": symbol, "interval": timeframe, "limit": int(limit)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    return _to_df(_get_json("/api/v3/klines", params))


def fetch_recent(symbol: str, timeframe: str, limit: int = 1000):
    """Returns (closed candles, the still-open candle or None). The open one is only used for the current price."""
    df = fetch_klines(symbol, timeframe, limit=limit)
    now = int(time.time() * 1000)
    closed = df[df["close_time"] < now].reset_index(drop=True)
    still_open = df[df["close_time"] >= now]
    current = still_open.iloc[-1] if len(still_open) else None
    return closed, current


def _fetch_range(symbol: str, timeframe: str, start_ms: int, end_ms: int, log) -> list[pd.DataFrame]:
    tf = timeframe_ms(timeframe)
    frames: list[pd.DataFrame] = []
    cur = int(start_ms)
    n = 0
    while cur < end_ms:
        chunk = fetch_klines(symbol, timeframe, start_ms=cur, end_ms=end_ms, limit=1000)
        if chunk.empty:
            break
        frames.append(chunk)
        n += len(chunk)
        if n % 10000 < 1000:
            log(f"  {symbol} {timeframe}: {n} candles fetched ...")
        if len(chunk) < 1000:
            break
        cur = int(chunk["open_time"].iloc[-1]) + tf
        time.sleep(0.12)
    return frames


def load_history(symbol: str, timeframe: str, days: int, cache_dir, log=print) -> pd.DataFrame:
    """Loads `days` of history. Whatever is already cached is not fetched again."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol}_{timeframe}.csv"
    tf = timeframe_ms(timeframe)
    now = int(time.time() * 1000)
    start = now - int(days) * 86_400_000
    start -= start % tf

    frames: list[pd.DataFrame] = []
    cached = None
    if path.exists() and path.stat().st_size > 0:
        cached = pd.read_csv(path)
        if cached.empty:
            cached = None
    if cached is not None:
        first = int(cached["open_time"].min())
        last = int(cached["open_time"].max())
        if first > start + tf:
            log(f"Fetching older {timeframe} data for {symbol} ...")
            frames += _fetch_range(symbol, timeframe, start, first - 1, log)
        frames += _fetch_range(symbol, timeframe, last + tf, now, log)
        frames.append(cached)
    else:
        log(f"Fetching {days} days of {timeframe} data for {symbol} from Binance (takes a moment) ...")
        frames += _fetch_range(symbol, timeframe, start, now, log)
    if not frames:
        raise RuntimeError(f"No data for {symbol}")
    df = pd.concat(frames, ignore_index=True)
    df = df[df["close_time"] < now]
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    df.to_csv(path, index=False)
    return df[df["open_time"] >= start].reset_index(drop=True)
