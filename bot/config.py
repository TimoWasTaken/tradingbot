"""Configuration and secrets loading, plus small time and number helpers shared by all bots."""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
SECRETS_PATH = ROOT / "secrets.json"

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Stockholm")   # time zone used for logs and notifications
except Exception:  # tzdata missing -> fall back to UTC
    LOCAL_TZ = dt.timezone.utc


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_secrets(path: Path = SECRETS_PATH) -> dict:
    """secrets.json holds everything private: exchange API keys and the private ntfy topic.
    It is never committed; see secrets.example.json."""
    defaults = {"binance_api_key": "", "binance_api_secret": "", "ntfy_topic": ""}
    if not Path(path).exists():
        return defaults
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {**defaults, **data}


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_local(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.timezone.utc).astimezone(LOCAL_TZ)


def fmt_ms(ms: int, seconds: bool = False) -> str:
    return ms_to_local(ms).strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def fmt_num(x: float, dec: int = 2) -> str:
    """Number formatting for messages: 78,459.44."""
    return f"{float(x):,.{dec}f}"


def period_word(tf: str) -> str:
    """Human word for one bar of the given timeframe, e.g. '1d' -> 'day'."""
    return {"1m": "minute", "5m": "5-minute bar", "15m": "15-minute bar", "30m": "30-minute bar",
            "1h": "hour", "2h": "2-hour bar", "4h": "4-hour bar", "1d": "day"}.get(tf, f"{tf} bar")
