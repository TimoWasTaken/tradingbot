"""Backtest the strategies on historical data.

    py backtest.py                                  crypto bot per config.json
    py backtest.py --config config_stocks_se.json   Swedish stocks bot (daily data from Yahoo Finance)
    py backtest.py --days 365                       shorter history
    py backtest.py --timeframe 1h                   other timeframe for crypto (5m, 15m, 30m, 1h, 2h, 4h, 1d)
    py backtest.py --symbols BTCUSDT                only some symbols (comma-separated)
    py backtest.py --strategy trend                 only one strategy (breakout, trend, meanrev)
    py backtest.py --open                           open the report in the browser when done
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot import data_stocks
from bot.backtest import run_backtest
from bot.config import ROOT, fmt_ms, load_config
from bot.data import load_history
from bot.report import backtest_report, verdict
from bot.strategies import load_strategies


def _f(x, dec: int = 2) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "inf" if isinstance(x, float) and math.isinf(x) else "-"
    return f"{x:.{dec}f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest on historical data.")
    ap.add_argument("--config", default="config.json", help="configuration file, e.g. config_stocks_se.json")
    ap.add_argument("--days", type=int, help="days of history")
    ap.add_argument("--timeframe", help="e.g. 15m, 1h, 4h (crypto only)")
    ap.add_argument("--symbols", help="comma-separated, e.g. BTCUSDT,ETHUSDT")
    ap.add_argument("--strategy", help="run only one strategy")
    ap.add_argument("--open", action="store_true", help="open the report in the browser")
    ap.add_argument("--with-kill-switch", action="store_true",
                    help="let the kill switch stop the test as in live (otherwise only the date it would fire is shown)")
    args = ap.parse_args()

    cfg = load_config(ROOT / args.config)
    market = cfg.get("market", "crypto")
    names = dict(cfg.get("symbol_names", {}))
    days = args.days or int(cfg.get("backtest", {}).get("days", 730))
    is_stocks = market in data_stocks.MARKETS
    tf = "1d" if is_stocks else (args.timeframe or cfg["timeframe"])
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else list(cfg["symbols"])
    strategies = load_strategies(cfg)
    if args.strategy:
        strategies = [s for s in strategies if s.name == args.strategy]
        if not strategies:
            print(f"Unknown or disabled strategy: {args.strategy}")
            return 1
    risk = cfg["risk"]
    start_cap = float(cfg["capital"]["start"])
    ccy = str(cfg["capital"].get("currency", "USDT"))
    fee_txt = f"Fee {risk['fee_pct']}%"
    if float(risk.get("min_fee", 0) or 0) > 0:
        fee_txt += f" (min {risk['min_fee']} {ccy})"
    print(f"\nBacktest {tf}, {days} days, {', '.join(names.get(s, s) for s in symbols)}.")
    print(f"Starting capital {start_cap:,.2f} {ccy}. {fee_txt} + slippage {risk['slippage_pct']}% per side, "
          f"max {risk['max_open_positions']} position(s) at a time.\n")

    benchmark = None
    benchmark_name = ""
    if is_stocks:
        wanted = list(symbols)
        if cfg.get("benchmark"):
            wanted.append(cfg["benchmark"])
        everything = data_stocks.load_history(wanted, days, log=print, market=data_stocks.get_market(market))
        data = {s: everything[s] for s in symbols if s in everything and not everything[s].empty}
        if cfg.get("benchmark") and not everything.get(cfg["benchmark"], data_stocks.pd.DataFrame()).empty:
            benchmark = everything[cfg["benchmark"]]
            benchmark_name = cfg.get("benchmark_name", "index")
        for s, df in data.items():
            print(f"  {names.get(s, s):18} {len(df):5} days, {fmt_ms(int(df['open_time'].iloc[0]))[:10]} "
                  f"to {fmt_ms(int(df['open_time'].iloc[-1]))[:10]}")
    else:
        data = {}
        for s in symbols:
            df = load_history(s, tf, days, ROOT / "data", log=print)
            data[s] = df
            print(f"  {s}: {len(df)} bars, {fmt_ms(int(df['open_time'].iloc[0]))} to {fmt_ms(int(df['open_time'].iloc[-1]))}")
    if not data:
        print("No data to test on.")
        return 1
    print()

    results = []
    for strat in strategies:
        print(f"Running {strat.title} ...")
        results.append(run_backtest(cfg, data, [strat], label=strat.title, kill_switch=args.with_kill_switch,
                                    benchmark=benchmark, benchmark_name=benchmark_name))
    print("Running all strategies together ...\n")
    combined = run_backtest(cfg, data, strategies, label="All strategies together",
                            kill_switch=args.with_kill_switch, benchmark=benchmark, benchmark_name=benchmark_name)

    hdr = f"{'Strategy':38} {'Return':>9} {'Trades':>8} {'Win%':>7} {'Payoff':>7} {'PF':>6} {'Exp/trade':>12} {'Max DD':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in results + [combined]:
        s = r.stats
        print(f"{r.label[:38]:38} {s['return_pct']:>+8.1f}% {s['count']:>8} {s.get('win_rate_pct', 0):>6.1f}% "
              f"{_f(s.get('payoff')):>7} {_f(s.get('profit_factor')):>6} {s.get('expectancy_pct', 0):>+11.2f}% "
              f"{s['max_dd_pct']:>+8.1f}%")
    print()
    for s, v in combined.buyhold.items():
        print(f"Buy & hold {names.get(s, s)}: {v:+.1f}%")
    print()
    _k, text = verdict(combined.stats, {names.get(s, s): v for s, v in combined.buyhold.items()})
    print("Verdict:", text)

    rep_dir = ROOT / "reports"
    rep_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    tag = cfg.get("journal", "")
    prefix = f"backtest_{tag}_{tf}" if tag else f"backtest_{tf}"
    meta = {
        "timeframe": tf, "days": days,
        "symbols": [names.get(s, s) for s in data],
        "heading": f"{tag} {tf}".strip(),
        "bars": max(len(df) for df in data.values()),
        "created": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "descriptions": {s.title: s.describe() for s in strategies},
    }
    html_text = backtest_report(cfg, results, combined, meta)
    path = rep_dir / f"{prefix}_{stamp}.html"
    path.write_text(html_text, encoding="utf-8")
    latest = rep_dir / (f"latest_backtest_{tag}.html" if tag else "latest_backtest.html")
    latest.write_text(html_text, encoding="utf-8")
    combined.trades.to_csv(rep_dir / f"{prefix}_{stamp}_trades.csv", index=False)
    print(f"\nReport saved: {path}")
    if args.open:
        os.startfile(path)  # noqa: S606 - opens the report in the default browser
    return 0


if __name__ == "__main__":
    sys.exit(main())
