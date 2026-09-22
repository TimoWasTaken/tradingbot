"""Builds an HTML report from a bot's journal.

    py report.py                               crypto bot (config.json)
    py report.py --config config_stocks_se.json
    py report.py --open                        open the report in the browser
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot.config import ROOT, load_config
from bot.journal import Journal, compute_stats
from bot.report import journal_report


def main() -> int:
    ap = argparse.ArgumentParser(description="Report from a journal.")
    ap.add_argument("--config", default="config.json", help="configuration file, e.g. config_stocks_se.json")
    ap.add_argument("--mode", choices=["paper", "live"])
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    cfg = load_config(ROOT / args.config)
    mode = args.mode or cfg.get("mode", "paper")
    journal_name = cfg.get("journal", mode)
    jdir = ROOT / "journal" / journal_name
    journal = Journal(jdir)
    trades = journal.load_trades()
    equity = journal.load_equity()
    state: dict = {}
    sp = jdir / "state.json"
    if sp.exists() and sp.stat().st_size > 0:
        state = json.loads(sp.read_text(encoding="utf-8"))
    tr = state.get("trader", {})
    start_cap = float(tr.get("start_equity", cfg["capital"]["start"]))
    ccy = str(cfg["capital"].get("currency", "USDT"))
    stats = compute_stats(trades, equity, start_cap)
    risk_state = state.get("risk", {})
    if "max_dd" in risk_state:
        stats["max_dd_pct"] = min(stats["max_dd_pct"], float(risk_state["max_dd"]))
    positions = list(tr.get("positions", {}).values())
    meta = {"created": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "prices": tr.get("last_prices", {}), "risk": risk_state,
            "pending": state.get("pending", []),
            "heading": journal_name if cfg.get("journal") else None}
    html_text = journal_report(cfg, mode, stats, trades, equity, positions, meta)
    out = ROOT / "reports" / f"report_{journal_name}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    print(f"{journal_name}: {stats['count']} trades, equity {stats['end_equity']:,.2f} {ccy} "
          f"({stats['return_pct']:+.2f}%), win rate {stats.get('win_rate_pct', 0):.0f}%, "
          f"expectancy {stats.get('expectancy_pct', 0):+.2f}%/trade, max drawdown {stats['max_dd_pct']:+.1f}%, "
          f"{len(positions)} open positions, {len(meta['pending'])} pending orders.")
    print(f"Report saved: {out}")
    if args.open:
        os.startfile(out)  # noqa: S606
    return 0


if __name__ == "__main__":
    sys.exit(main())
