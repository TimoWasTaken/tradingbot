"""Daily stock data from Yahoo Finance (free, roughly 15 minutes delayed).
Tickers are written as on Yahoo: VOLV-B.ST for Stockholm, AAPL for New York, ^OMX and ^GSPC for indices.
Prices are adjusted for dividends so the backtest does not see fake drops on ex-dividend days.

Each exchange is described by a Market: time zone, open, close and when today's bar counts as final."""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

import pandas as pd

from .config import LOCAL_TZ

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Market:
    name: str
    tz_name: str
    open: dt.time
    close: dt.time
    settle: dt.time          # from this local time today's bar is treated as closed (after the closing auction)
    open_label: str          # how the open is described in messages
    settle_label: str        # when signals arrive, for messages

    @property
    def tz(self):
        if ZoneInfo is None:
            return LOCAL_TZ
        try:
            return ZoneInfo(self.tz_name)
        except Exception:
            return LOCAL_TZ

    def ms(self, d: dt.date, t: dt.time) -> int:
        return int(dt.datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=self.tz).timestamp() * 1000)

    def day_ms(self, d: dt.date) -> int:
        """open_time of a trading day."""
        return self.ms(d, self.open)

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)


MARKETS = {
    "stocks_se": Market("stocks_se", "Europe/Stockholm", dt.time(9, 0), dt.time(17, 30), dt.time(17, 35),
                        "09:00 Stockholm time", "about 17:35 Stockholm time"),
    "stocks_us": Market("stocks_us", "America/New_York", dt.time(9, 30), dt.time(16, 0), dt.time(16, 5),
                        "09:30 New York time (15:30 Stockholm)", "about 16:05 New York time (22:05 Stockholm)"),
}
STOCKHOLM = MARKETS["stocks_se"]
KEEP = ["open_time", "open", "high", "low", "close", "volume", "close_time"]


def get_market(name: str) -> Market:
    if name not in MARKETS:
        raise ValueError(f"Unknown market '{name}'. Known: {', '.join(MARKETS)}")
    return MARKETS[name]


def _to_df(raw: pd.DataFrame, market: Market) -> pd.DataFrame:
    """Yahoo layout (Open/High/Low/Close/Volume with a date index) -> the bots' candle layout."""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=KEEP)
    df = raw.copy()
    df.columns = [str(c).lower() for c in df.columns]
    needed = ["open", "high", "low", "close"]
    if any(c not in df.columns for c in needed):
        return pd.DataFrame(columns=KEEP)
    df = df.dropna(subset=needed)
    df = df[df["close"] > 0]
    if len(df) == 0:
        return pd.DataFrame(columns=KEEP)
    if "volume" not in df.columns:
        df["volume"] = 0.0
    dates = [pd.Timestamp(x).date() for x in df.index]
    out = pd.DataFrame({
        "open_time": [market.day_ms(d) for d in dates],
        "open": df["open"].astype(float).to_numpy(),
        "high": df["high"].astype(float).to_numpy(),
        "low": df["low"].astype(float).to_numpy(),
        "close": df["close"].astype(float).to_numpy(),
        "volume": df["volume"].fillna(0).astype(float).to_numpy(),
        "close_time": [market.ms(d, market.close) for d in dates],
    })
    return out.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def _download(tickers: list[str], market: Market, start: str | None = None, period: str | None = None,
              tries: int = 3) -> dict[str, pd.DataFrame]:
    import yfinance as yf  # imported here so the crypto bot does not need the package

    tickers = list(dict.fromkeys(tickers))
    last: Exception | None = None
    data = None
    for attempt in range(tries):
        try:
            kwargs: dict = dict(interval="1d", auto_adjust=True, group_by="ticker", threads=False, progress=False)
            if start:
                kwargs["start"] = start
            else:
                kwargs["period"] = period or "2y"
            data = yf.download(tickers, **kwargs)
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"Could not fetch data from Yahoo Finance: {last}")
    out: dict[str, pd.DataFrame] = {}
    if len(data) == 0:
        return {t: pd.DataFrame(columns=KEEP) for t in tickers}
    if isinstance(data.columns, pd.MultiIndex):
        lvl0 = set(data.columns.get_level_values(0))
        for t in tickers:
            out[t] = _to_df(data[t], market) if t in lvl0 else pd.DataFrame(columns=KEEP)
    else:
        out[tickers[0]] = _to_df(data, market)
        for t in tickers[1:]:
            out[t] = pd.DataFrame(columns=KEEP)
    return out


def load_history(tickers: list[str], days: int, log=print, market: Market = STOCKHOLM) -> dict[str, pd.DataFrame]:
    """History for backtests. Today's unfinished bar is dropped while the market is still open."""
    start = (dt.date.today() - dt.timedelta(days=int(days))).isoformat()
    log(f"Fetching {days} days of daily data for {len(tickers)} tickers from Yahoo Finance ...")
    data = _download(tickers, market, start=start)
    now = market.now()
    if now.time() < market.settle:
        today = market.day_ms(now.date())
        data = {t: df[df["open_time"] != today].reset_index(drop=True) for t, df in data.items()}
    for t, df in data.items():
        if df.empty:
            log(f"  WARNING: no data for {t}. Check the ticker on finance.yahoo.com")
    return data


def fetch_recent(tickers: list[str], period: str = "2y",
                 market: Market = STOCKHOLM) -> dict[str, tuple[pd.DataFrame, pd.Series | None]]:
    """Per ticker: (closed daily bars, today's still-open bar or None).
    Today's bar counts as closed from market.settle, local exchange time."""
    now = market.now()
    today = market.day_ms(now.date())
    raw = _download(tickers, market, period=period)
    out: dict[str, tuple[pd.DataFrame, pd.Series | None]] = {}
    for t, df in raw.items():
        if df.empty:
            out[t] = (df, None)
            continue
        is_today = df["open_time"] == today
        if is_today.any() and now.time() < market.settle:
            out[t] = (df[~is_today].reset_index(drop=True), df[is_today].iloc[-1])
        else:
            out[t] = (df.reset_index(drop=True), None)
    return out


def market_is_open(market: Market = STOCKHOLM, now: dt.datetime | None = None) -> bool:
    now = now or market.now()
    return now.weekday() < 5 and market.open <= now.time() < market.close
