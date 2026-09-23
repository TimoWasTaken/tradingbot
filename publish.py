"""Builds the public track-record page from the paper-trading journals and pushes it to GitHub Pages.

    py publish.py            build public/index.html (+ CSV data) and push if a GitHub user is configured
    py publish.py --no-push  build only

Settings live in config_publish.json (github_user, repo, branch, public_topic). The page contains only the
journals (trades, equity, positions) and plain-English rule descriptions. No code, keys or private topics.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from bot.config import ROOT, fmt_ms, load_config
from bot.journal import Journal, compute_stats
from bot.report import CSS, REASON_LABEL, svg_chart

OUT = ROOT / "public"
STRAT_LABEL = {"breakout": "Breakout", "trend": "Trend", "meanrev": "Mean reversion"}

BOTS = [
    {
        "key": "crypto", "cfg": "config.json", "title": "Crypto bot", "ccy": "USDT",
        "subtitle": "Binance spot, Bitcoin and Ethereum vs USDT, 4-hour bars. Paper trading since 2026-09-08.",
        "rules": ("Three long-only strategies: volume breakout (close above the 20-bar high on 1.5x average volume, exit "
                  "below the 10-bar low), EMA20/EMA50 trend cross, and Bollinger/RSI mean reversion. All require price "
                  "above the 200-bar EMA. ATR-based stop-loss and trailing stop, max 1 position, 2% risk per trade, "
                  "daily-loss halt at -5%, kill switch at -30% drawdown, half-size 'cautious mode' below -10% drawdown. "
                  "Fees 0.1% and slippage 0.05% per side. Paper capital 10 USDT."),
        "backtest": "Backtest, 4 years to Sept 2026: +80% (profit factor 1.44, max drawdown -20%). Bitcoin buy & hold: +269%.",
    },
    {
        "key": "stocks_se", "cfg": "config_stocks_se.json", "title": "Swedish stocks bot (signals only)", "ccy": "SEK",
        "subtitle": "40 large caps on Nasdaq Stockholm, daily bars. Paper trading since 2026-09-08.",
        "rules": ("Donchian breakout (close above the 55-day high, exit below the 20-day low) and EMA50/EMA200 trend "
                  "cross, both requiring price above the 200-day EMA, 3 to 4 ATR stops. Signals are generated at the "
                  "close and filled at the next day's open. Max 5 positions of roughly 2,000 SEK, 1% risk per trade, "
                  "daily-loss halt -3%, kill switch -20%. Avanza Mini commission 0.25% (min 1 SEK) and 0.05% slippage "
                  "per side. Paper capital 10,000 SEK. Avanza has no API, so this bot only publishes signals."),
        "backtest": "Backtest, 10 years: +64% (profit factor 1.4, max drawdown -15%). OMXS30 buy & hold: +130%.",
    },
    {
        "key": "stocks_us", "cfg": "config_stocks_us.json", "title": "US stocks bot (signals only)", "ccy": "USD",
        "subtitle": "40 US large caps, daily bars. Paper trading since 2026-09-22.",
        "rules": ("EMA50/EMA200 trend cross only; the breakout strategy was removed after losing money in the backtest "
                  "once currency-exchange costs were included. 3 ATR stop, 5 ATR trailing stop. Signals at the close, "
                  "filled at the next open. Max 5 positions, fees 0.5% per side (commission plus FX). Paper capital 1,000 USD."),
        "backtest": "Backtest, 10 years: +70% (profit factor 2.1, max drawdown -11%). S&P 500 buy & hold: +257%.",
    },
]
FUND = {
    "key": "funds", "cfg": "config_funds.json", "title": "Fund rotation bot (dual momentum)",
    "subtitle": "Six broad asset classes, one decision per month. Paper trading since 2026-09-22.",
    "rules": ("At each month-end close, rank US stocks, European stocks, emerging markets, Swedish stocks, gold and "
              "long-term bonds by the average of their 3-, 6- and 12-month returns. Hold the top two, equal weight. "
              "Any slot that does not beat short-term bonds goes to short-term bonds. Executed at the next close. "
              "Measured on US ETFs (SPY, VGK, EEM, EWD, GLD, IEF, SHY); signals name commission-free Avanza funds. "
              "0.3% per year fund fee and 0.1% per switch deducted. Currency moves SEK/USD not included. Paper capital 10,000 SEK."),
    "backtest": ("Backtest 2006 to 2026: +7.0% per year, max drawdown -25%, worst year -21%. "
                 "S&P 500 buy & hold: +10.9% per year, max drawdown -55%. Swedish stocks: +5.5% per year, max drawdown -68%."),
}


def esc(s) -> str:
    return html.escape(str(s))


def cls(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return ""
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def n(x, dec=2) -> str:
    try:
        return f"{float(x):,.{dec}f}"
    except (TypeError, ValueError):
        return "–"


def cards(items) -> str:
    return '<div class="cards">' + "".join(
        f'<div class="card"><div class="l">{esc(l)}</div><div class="v {c}">{v}</div></div>' for l, v, c in items) + "</div>"


def chart(ms, values, start: float, label="Bot") -> str:
    if len(values) < 2:
        return "<p class='small'>Not enough data for a chart yet.</p>"
    series = [(label, [v / start * 100 for v in values], "#1f6feb")]
    k = len(ms)
    xl = [(f, fmt_ms(int(ms[int(round(f * (k - 1)))]))[:10]) for f in (0, 0.25, 0.5, 0.75, 1.0)]
    return ('<div class="chart">' + svg_chart(series, xl) +
            "<p class='small'>Start = 100. Marked to market at each closed bar.</p></div>")


def load_state(jdir: Path) -> dict:
    p = jdir / "state.json"
    if p.exists() and p.stat().st_size > 0:
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def bot_section(b: dict) -> str:
    cfg = load_config(ROOT / b["cfg"])
    names = dict(cfg.get("symbol_names", {}))
    jdir = ROOT / "journal" / b["key"]
    journal = Journal(jdir)
    trades = journal.load_trades()
    equity = journal.load_equity()
    state = load_state(jdir)
    tr = state.get("trader", {})
    start = float(tr.get("start_equity", cfg["capital"]["start"]))
    stats = compute_stats(trades, equity, start)
    risk = state.get("risk", {})
    if "max_dd" in risk:
        stats["max_dd_pct"] = min(stats["max_dd_pct"], float(risk["max_dd"]))
    ccy = b["ccy"]
    if len(trades):
        t = trades.copy()
        t["strategy"] = t["strategy"].map(lambda s: STRAT_LABEL.get(s, s))
        t["reason"] = t["reason"].map(lambda s: REASON_LABEL.get(s, s))
        t.to_csv(OUT / "data" / f"{b['key']}_trades.csv", index=False)
    else:
        (OUT / "data" / f"{b['key']}_trades.csv").write_text(",".join(trades.columns) + "\n", encoding="utf-8")
    if len(equity):
        equity.to_csv(OUT / "data" / f"{b['key']}_equity.csv", index=False)
    cur = float(stats["end_equity"])
    dec = 0 if ccy == "SEK" else 2
    items = [
        ("Paper capital", f"{n(cur, dec)} {ccy}", ""),
        ("Since start", f"{stats['return_pct']:+.2f}%", cls(stats["return_pct"])),
        ("Closed trades", str(int(stats["count"])), ""),
        ("Win rate", f"{stats.get('win_rate_pct', 0):.0f}%" if stats["count"] else "–", ""),
        ("Avg per trade", f"{stats.get('expectancy_pct', 0):+.2f}%" if stats["count"] else "–", cls(stats.get("expectancy_pct", 0))),
        ("Profit factor", (f"{stats['profit_factor']:.2f}" if stats["count"] and stats["profit_factor"] != float("inf") else "–"), ""),
        ("Max drawdown", f"{stats['max_dd_pct']:.1f}%", "neg"),
        ("Fees paid", f"{n(stats.get('fees', 0))} {ccy}", ""),
    ]
    flags = []
    if risk.get("killed"):
        flags.append("<div class='verdict bad'><b>Kill switch active.</b> The bot has closed everything and stopped.</div>")
    if risk.get("cautious"):
        flags.append("<div class='verdict'>Cautious mode: drawdown beyond -10%, new positions at half size.</div>")
    pos_rows = ""
    prices = tr.get("last_prices", {})
    for p in tr.get("positions", {}).values():
        last = float(prices.get(p["symbol"], p["entry_price"]))
        upct = (last / float(p["entry_price"]) - 1) * 100
        pos_rows += (f"<tr><td>{esc(names.get(p['symbol'], p['symbol']))}</td><td>{esc(STRAT_LABEL.get(p['strategy'], p['strategy']))}</td>"
                     f"<td>{esc(fmt_ms(p['entry_ms']))}</td><td>{n(p['entry_price'])}</td><td>{n(last)}</td><td>{n(p['stop'])}</td>"
                     f"<td>{n(p['cost'], dec)} {ccy}</td><td class='{cls(upct)}'>{upct:+.2f}%</td></tr>")
    positions = ("<div class='scroll'><table><tr><th>Symbol</th><th>Strategy</th><th>Entered</th><th>Entry</th><th>Last</th>"
                 "<th>Stop</th><th>Size</th><th>Unrealized</th></tr>" + pos_rows + "</table></div>") if pos_rows else "<p class='small'>No open positions.</p>"
    pend_rows = ""
    for p in state.get("pending", []):
        pend_rows += (f"<tr><td>{'Buy' if p['kind'] == 'buy' else 'Sell'}</td><td>{esc(names.get(p['symbol'], p['symbol']))}</td>"
                      f"<td>{esc(STRAT_LABEL.get(p['strategy'], p['strategy']))}</td><td>{esc(fmt_ms(p['signal_ms'])[:10])}</td></tr>")
    pending = ("<h4>Pending orders (fill at next open)</h4><div class='scroll'><table><tr><th>Order</th><th>Symbol</th>"
               "<th>Strategy</th><th>Signal date</th></tr>" + pend_rows + "</table></div>") if pend_rows else ""
    trade_rows = ""
    for _, r in trades.tail(100).iloc[::-1].iterrows():
        trade_rows += (f"<tr><td>{int(r['id'])}</td><td>{esc(names.get(r['symbol'], r['symbol']))}</td>"
                       f"<td>{esc(STRAT_LABEL.get(r['strategy'], r['strategy']))}</td><td>{esc(r['entry_time'])}</td><td>{n(r['entry_price'])}</td>"
                       f"<td>{esc(r['exit_time'])}</td><td>{n(r['exit_price'])}</td><td>{n(r['cost'], dec)}</td>"
                       f"<td class='{cls(r['pnl'])}'>{float(r['pnl']):+.2f}</td><td class='{cls(r['pnl_pct'])}'>{float(r['pnl_pct']):+.2f}%</td>"
                       f"<td>{esc(REASON_LABEL.get(str(r['reason']), r['reason']))}</td><td>{int(r['bars'])}</td></tr>")
    trades_html = ("<div class='scroll'><table><tr><th>#</th><th>Symbol</th><th>Strategy</th><th>Entry time</th><th>Entry</th>"
                   "<th>Exit time</th><th>Exit</th><th>Size</th><th>P/L</th><th>%</th><th>Exit reason</th><th>Bars</th></tr>"
                   + trade_rows + "</table></div>") if trade_rows else "<p class='small'>No closed trades yet.</p>"
    eq_chart = chart(equity["ms"].to_numpy(), equity["equity"].astype(float).to_numpy(), start) if len(equity) else \
        "<p class='small'>No equity data yet.</p>"
    return (f"<h2 id='{b['key']}'>{esc(b['title'])}</h2><p class='meta'>{esc(b['subtitle'])}</p>"
            f"<p><b>Rules.</b> {esc(b['rules'])}</p><p class='small'><b>Backtest (hypothetical, not live).</b> {esc(b['backtest'])}</p>"
            + "".join(flags) + cards(items) + "<h4>Equity curve (paper)</h4>" + eq_chart
            + "<h4>Open positions</h4>" + positions + pending + "<h4>Closed trades (latest 100)</h4>" + trades_html
            + f"<p class='small'>Data: <a href='data/{b['key']}_trades.csv'>trades.csv</a>, <a href='data/{b['key']}_equity.csv'>equity.csv</a></p>")


def fund_section(f: dict) -> str:
    cfg = load_config(ROOT / f["cfg"])
    asset_names = dict(cfg.get("ticker_names", {}))
    funds = dict(cfg.get("fund_names", {}))
    jdir = ROOT / "journal" / f["key"]
    start = float(cfg["capital"]["start"])
    state = load_state(jdir)
    ep, sp = jdir / "equity.csv", jdir / "switches.csv"
    items = []
    eq_html = "<p class='small'>No equity data yet.</p>"
    if ep.exists() and ep.stat().st_size > 0:
        k = pd.read_csv(ep)
        k.to_csv(OUT / "data" / "funds_equity.csv", index=False)
        eq = k["equity"].astype(float)
        dd = float((eq / eq.cummax() - 1).min() * 100)
        items = [("Paper capital", f"{n(eq.iloc[-1], 0)} SEK", ""), ("Since start", f"{(eq.iloc[-1] / start - 1) * 100:+.2f}%", cls(eq.iloc[-1] - start)),
                 ("Max drawdown", f"{dd:.1f}%", "neg"), ("Switches", str(int(state.get("switches", 0))), "")]
        eq_html = chart(k["ms"].to_numpy(), eq.to_numpy(), start)
    hold = ", ".join(f"{asset_names.get(t, t)} ({funds.get(t, t)})" for t in state.get("current", [])) or "nothing yet (first switch pending)"
    pend = ""
    if state.get("pending"):
        pend = "<p class='small'>Pending switch to: " + esc(", ".join(f"{asset_names.get(t, t)} ({funds.get(t, t)})" for t in state["pending"]["chosen"])) + "</p>"
    sw = ""
    if sp.exists() and sp.stat().st_size > 0:
        b = pd.read_csv(sp)
        b.to_csv(OUT / "data" / "funds_switches.csv", index=False)
        rows = "".join(f"<tr><td>{esc(r['time'])}</td><td>{esc(r['portfolio'])}</td><td>{n(r['value'], 0)} SEK</td></tr>" for _, r in b.iloc[::-1].iterrows())
        sw = "<h4>Switches</h4><div class='scroll'><table><tr><th>Time</th><th>Portfolio</th><th>Value</th></tr>" + rows + "</table></div>"
    return (f"<h2 id='funds'>{esc(f['title'])}</h2><p class='meta'>{esc(f['subtitle'])}</p><p><b>Rules.</b> {esc(f['rules'])}</p>"
            f"<p class='small'><b>Backtest (hypothetical, not live).</b> {esc(f['backtest'])}</p>" + (cards(items) if items else "")
            + f"<h4>Current holdings</h4><p>{esc(hold)}</p>" + pend + "<h4>Equity curve (paper)</h4>" + eq_html + sw)


POLY = {
    "key": "polymarket", "cfg": "config_polymarket.json", "title": "Polymarket paper bettor",
    "subtitle": "Prediction markets, simulated bankroll of 100 USD. Paper betting since 2026-09-22.",
    "rules": ("Scans Polymarket's public API every hour for two documented edges. (1) Favorites: buys the side priced "
              "between 90 and 98.5 cents in liquid, non-sports markets that resolve within 45 days, 10% of bankroll per "
              "bet, max 8 open, max 2 per event and 4 per category. Research on 588 million Polymarket trades found such "
              "favorites paid about +0.3 to +1 cent per dollar more than their price implied, while longshots under "
              "10 cents lost 6 to 20 cents per dollar; sports showed no such bias and is excluded. (2) Arbitrage: when "
              "the YES bids of mutually exclusive outcomes sum to more than 1, buys NO on all of them, which costs less "
              "than the guaranteed payout. Bets are held to resolution; one tick of slippage and Polymarket's taker fee "
              "(where the market charges one) are deducted on every fill. No real orders are ever placed."),
    "backtest": ("No backtest of our own yet. The bot records a daily snapshot of every scanned market so the "
                 "favorite-longshot bias can be measured on its own data over time (see the research command)."),
}


def poly_section(f: dict) -> str:
    cfg = load_config(ROOT / f["cfg"])
    jdir = ROOT / "journal" / f["key"]
    start = float(cfg["capital"]["start"])
    state = load_state(jdir)
    bets_p, eq_p = jdir / "bets.csv", jdir / "equity.csv"
    items, eq_html, sw = [], "<p class='small'>No data yet.</p>", ""
    bets = pd.read_csv(bets_p) if bets_p.exists() and bets_p.stat().st_size > 0 else pd.DataFrame()
    if eq_p.exists() and eq_p.stat().st_size > 0:
        k = pd.read_csv(eq_p)
        k.to_csv(OUT / "data" / "polymarket_equity.csv", index=False)
        e = k["equity"].astype(float)
        dd = float((e / e.cummax() - 1).min() * 100)
        items = [("Paper bankroll", f"{n(e.iloc[-1])} USD", ""), ("Since start", f"{(e.iloc[-1] / start - 1) * 100:+.2f}%", cls(e.iloc[-1] - start)),
                 ("Max drawdown", f"{dd:.1f}%", "neg"), ("Settled bets", str(len(bets)), "")]
        if len(bets):
            won = int((bets["result"] == "won").sum())
            items += [("Win rate", f"{won / len(bets) * 100:.0f}%", ""), ("Avg per bet", f"{bets['pnl_pct'].mean():+.2f}%", cls(bets["pnl_pct"].mean()))]
        eq_html = chart(k["ms"].to_numpy(), e.to_numpy(), start)
    open_rows = "".join(f"<tr><td>{p['id']}</td><td>{esc(p['kind'])}</td><td>{esc(p['side_name'])}</td><td>{esc(p['question'])}</td>"
                        f"<td>{float(p['price']):.3f}</td><td>{n(p['stake'])}</td><td>{esc(p['end_date'])}</td></tr>"
                        for p in state.get("positions", []))
    open_html = ("<div class='scroll'><table><tr><th>#</th><th>Kind</th><th>Side</th><th>Question</th><th>Price</th><th>Stake USD</th>"
                 "<th>Resolves</th></tr>" + open_rows + "</table></div>") if open_rows else "<p class='small'>No open bets.</p>"
    if len(bets):
        bets.to_csv(OUT / "data" / "polymarket_bets.csv", index=False)
        rows = "".join(f"<tr><td>{int(r['id'])}</td><td>{esc(r['kind'])}</td><td>{esc(r['side'])}</td><td>{esc(r['question'])}</td>"
                       f"<td>{float(r['price']):.3f}</td><td>{n(r['stake'])}</td><td class='{cls(float(r['pnl']))}'>{float(r['pnl']):+.2f}</td>"
                       f"<td class='{cls(float(r['pnl_pct']))}'>{float(r['pnl_pct']):+.1f}%</td><td>{esc(r['result'])}</td></tr>"
                       for _, r in bets.tail(100).iloc[::-1].iterrows())
        sw = ("<h4>Settled bets</h4><div class='scroll'><table><tr><th>#</th><th>Kind</th><th>Side</th><th>Question</th><th>Price</th>"
              "<th>Stake</th><th>P/L USD</th><th>%</th><th>Result</th></tr>" + rows + "</table></div>")
    arb_html = ""
    ck = jdir / "arb_check.csv"
    if ck.exists() and ck.stat().st_size > 0:
        c = pd.read_csv(ck)
        c.to_csv(OUT / "data" / "polymarket_arb_check.csv", index=False)
        hp = jdir / "arb_hits.csv"
        hits = pd.read_csv(hp) if hp.exists() and hp.stat().st_size > 0 else pd.DataFrame()
        if len(hits):
            hits.to_csv(OUT / "data" / "polymarket_arb_hits.csv", index=False)
        arb_items = [("Order-book checks", str(len(c)), ""), ("Since", str(c["time"].iloc[0])[:16], ""),
                     ("Markets per check", f"{c['checked'].mean():.0f}", ""), ("Sum below 1", str(int(c["sum_below_1"].sum())), ""),
                     ("Positive after fees", str(int(c["net_positive"].sum())), ""), ("Best sum ever", f"{c['best_sum'].min():.3f}", ""),
                     ("Typical sum", f"{c['median_sum'].median():.3f}", "")]
        arb_html = ("<h4>Same-market arbitrage check</h4><p class='small'>Every 5 minutes the bot pulls the real order books of the "
                    "200 most traded Yes/No markets and tests the \"YES ask + NO ask below 1\" opportunity that public Polymarket bots "
                    "advertise as guaranteed profit. Counted: how often it exists at all, and how often anything is left after the taker "
                    "fee. A sum of 1.010 means buying both sides costs 1.01 to get 1 back.</p>" + cards(arb_items))
    return (f"<h2 id='polymarket'>{esc(f['title'])}</h2><p class='meta'>{esc(f['subtitle'])}</p><p><b>Rules.</b> {esc(f['rules'])}</p>"
            f"<p class='small'><b>Backtest.</b> {esc(f['backtest'])}</p>" + (cards(items) if items else "")
            + "<h4>Bankroll (paper)</h4>" + eq_html + "<h4>Open bets</h4>" + open_html + sw + arb_html)


CRYPTO15 = {
    "key": "crypto15", "cfg": "config_crypto15.json", "title": "Crypto 15-minute markets: the speed test",
    "subtitle": "Polymarket's Bitcoin and Ethereum Up-or-Down windows watched every 5 seconds against Binance. Paper since 2026-09-22.",
    "rules": ("The bots that demonstrably profit on Polymarket trade these windows on speed: Binance moves first, Polymarket "
              "reprices later, and Polymarket now charges takers 0.07 x price x (1 - price) per share here. This test asks how "
              "much of that edge is left at home-computer speed. (1) Fair value: from the Binance move since the window opened, "
              "the time left and the last hour's realised volatility it computes the probability the window ends UP, and buys a "
              "side when its ask is at least 4 cents below that after fee and one tick of slippage (5% of a 100 USD paper "
              "bankroll, one bet per window and coin, held to resolution). (2) DipArb replay: the rule from public Polymarket bot "
              "repositories, buy a side whose ask fell 15% within seconds, then buy the other side within 60 seconds if the pair "
              "costs 0.92 or less; 20 shares per dip, stop-loss if no hedge appears, counted without a bankroll limit so the "
              "statistic keeps growing (it lost 92 USD in its first 90 minutes). "
              "Every tick, bet, dip and window resolution is journaled."),
}


COPYTRADE = {
    "key": "copytrade", "cfg": "config_copytrade.json", "title": "Copy-trading test",
    "subtitle": "Follows Polymarket leaderboard wallets with 100 USD of paper money. Since 2026-09-23.",
    "rules": ("The third idea public Polymarket bots sell: copy the leaderboard. Once a day the overall and politics leaderboards "
              "(month and week) are judged on each wallet's last 100 closed positions with the gate those bots advertise (60%+ win "
              "rate, profit factor 1.5+, 30+ closed positions, no single position above 30% of the profit), plus two practical "
              "conditions: traded within a week, and not mostly 5/15-minute crypto windows, which are over before a copy could "
              "fill. Up to 15 wallets are followed; their recent trades are polled every minute. A buy is copied at the live best "
              "ask plus one tick and the taker fee with 5% of the bankroll, unless the market ends within an hour, the price is "
              "already more than 10% above theirs, or the signal is older than 15 minutes. A sell by the same wallet closes the "
              "copy at the best bid; otherwise copies ride to resolution. Every signal is logged with the delay and price premium."),
}


def copytrade_section(f: dict) -> str:
    from bot import copytrade as ct
    cfg = load_config(ROOT / f["cfg"])
    jdir = ROOT / "journal" / f["key"]
    start = float(cfg["capital"]["start"])
    bets, signals, eq = (ct.load_csv(jdir / x) for x in ("bets.csv", "signals.csv", "equity.csv"))
    state = load_state(jdir)
    items, eq_html = [], "<p class='small'>No data yet.</p>"
    if len(eq):
        eq.to_csv(OUT / "data" / "copytrade_equity.csv", index=False)
        e = eq["equity"].astype(float)
        items += [("Paper bankroll", f"{n(e.iloc[-1])} USD", ""), ("Since start", f"{(e.iloc[-1] / start - 1) * 100:+.2f}%", cls(e.iloc[-1] - start))]
        eq_html = chart(eq["ms"].to_numpy(), e.to_numpy(), start)
    if len(bets):
        bets.to_csv(OUT / "data" / "copytrade_bets.csv", index=False)
        won = int((bets["result"] == "won").sum())
        items += [("Closed copies", str(len(bets)), ""), ("Win rate", f"{won / len(bets) * 100:.0f}%", ""),
                  ("Avg per copy", f"{bets['pnl_pct'].mean():+.1f}%", cls(bets["pnl_pct"].mean()))]
    if len(signals):
        signals.to_csv(OUT / "data" / "copytrade_signals.csv", index=False)
        buys = signals[signals["side"] == "BUY"]
        copied = signals[signals["action"] == "copied"]
        items += [("Signals seen", str(len(signals)), ""), ("Buys copied", f"{len(copied)} of {len(buys)}", ""),
                  ("Median delay", f"{signals['delay_s'].median() / 60:.0f} min", "")]
        prem = pd.to_numeric(copied["premium_pct"], errors="coerce").dropna()
        if len(prem):
            items += [("Avg price premium", f"{prem.mean():+.1f}%", cls(-prem.mean()))]
    items += [("Wallets followed", str(len([w for w in state.get("wallets", {}).values() if not w.get("retired")])), "")]
    wrows = "".join(f"<tr><td>{esc(v['name'])}</td><td>{esc(v.get('board', ''))}</td><td>{float(v.get('pnl', 0)):,.0f}</td>"
                    f"<td>{float(v.get('win_rate', 0)) * 100:.0f}%</td><td>{float(v.get('profit_factor', 0)):.2f}</td><td>{esc(v.get('closed_n', ''))}</td></tr>"
                    for v in state.get("wallets", {}).values() if not v.get("retired"))
    w_html = ("<h4>Wallets followed (public leaderboard names)</h4><div class='scroll'><table><tr><th>Name</th><th>Board</th><th>Period P/L USD</th>"
              "<th>Win rate</th><th>Profit factor</th><th>Closed positions judged</th></tr>" + wrows + "</table></div>") if wrows else ""
    open_rows = "".join(f"<tr><td>{p['id']}</td><td>{esc(p['name'])}</td><td>{esc(p['outcome'])}</td><td>{esc(p['question'])}</td><td>{float(p['their_price']):.2f}</td>"
                        f"<td>{float(p['price']):.2f}</td><td>{n(p['stake'])}</td><td>{p['delay_s'] / 60:.0f} min</td><td>{esc(p.get('end_date', ''))}</td></tr>"
                        for p in state.get("positions", []))
    open_html = ("<div class='scroll'><table><tr><th>#</th><th>Wallet</th><th>Side</th><th>Market</th><th>Their price</th><th>Our price</th>"
                 "<th>Stake USD</th><th>Delay</th><th>Resolves</th></tr>" + open_rows + "</table></div>") if open_rows else "<p class='small'>No open copies.</p>"
    b_html = ""
    if len(bets):
        rows = "".join(f"<tr><td>{int(r['id'])}</td><td>{esc(r['name'])}</td><td>{esc(r['outcome'])}</td><td>{esc(r['question'])}</td><td>{float(r['their_price']):.2f}</td>"
                       f"<td>{float(r['price']):.2f}</td><td>{esc(r['exit'])}</td><td class='{cls(float(r['pnl']))}'>{float(r['pnl']):+.2f}</td></tr>"
                       for _, r in bets.tail(60).iloc[::-1].iterrows())
        b_html = ("<h4>Closed copies</h4><div class='scroll'><table><tr><th>#</th><th>Wallet</th><th>Side</th><th>Market</th><th>Their price</th>"
                  "<th>Our price</th><th>Exit</th><th>P/L USD</th></tr>" + rows + "</table></div>")
    return (f"<h2 id='copytrade'>{esc(f['title'])}</h2><p class='meta'>{esc(f['subtitle'])}</p><p><b>Rules.</b> {esc(f['rules'])}</p>"
            + cards(items) + "<h4>Bankroll (paper)</h4>" + eq_html + w_html + "<h4>Open copies</h4>" + open_html + b_html)


def crypto15_section(f: dict) -> str:
    from bot import crypto15 as c15
    cfg = load_config(ROOT / f["cfg"])
    jdir = ROOT / "journal" / f["key"]
    start = float(cfg["capital"]["start"])
    dip_start = float(cfg.get("diparb", {}).get("start_cash", start))
    bets, dips, eq, rounds = (c15.load_csv(jdir / x) for x in ("bets.csv", "dips.csv", "equity.csv", "rounds.csv"))
    items, eq_html = [], "<p class='small'>No data yet.</p>"
    if len(eq):
        eq.to_csv(OUT / "data" / "crypto15_equity.csv", index=False)
        e, d = eq["equity"].astype(float), eq["dip_equity"].astype(float)
        items += [("Fair-value bankroll", f"{n(e.iloc[-1])} USD", ""), ("Since start", f"{(e.iloc[-1] / start - 1) * 100:+.2f}%", cls(e.iloc[-1] - start)),
                  ("DipArb replay P/L", f"{d.iloc[-1] - dip_start:+.2f} USD", cls(d.iloc[-1] - dip_start))]
        k = len(eq)
        xl = [(q, fmt_ms(int(eq["ms"].iloc[int(round(q * (k - 1)))]))[:16]) for q in (0, 0.25, 0.5, 0.75, 1.0)] if k > 1 else None
        eq_html = ("<h4>Fair-value bankroll, % of start (paper)</h4><div class='chart'>" + svg_chart([("Fair value", [v / start * 100 for v in e], "#1f6feb")], xl)
                   + "</div><h4>DipArb replay, cumulative P/L in USD</h4><div class='chart'>"
                   + svg_chart([("DipArb replay", [v - dip_start for v in d], "#d29922")], xl) + "</div>")
    if len(bets):
        bets.to_csv(OUT / "data" / "crypto15_bets.csv", index=False)
        won = int((bets["result"] == "won").sum())
        items += [("Settled bets", str(len(bets)), ""), ("Win rate", f"{won / len(bets) * 100:.0f}%", ""),
                  ("Avg per bet", f"{bets['pnl_pct'].mean():+.1f}%", cls(bets["pnl_pct"].mean()))]
    if len(dips):
        dips.to_csv(OUT / "data" / "crypto15_dips.csv", index=False)
        items += [("Dips seen", str(len(dips)), ""), ("Hedged", str(int((dips["outcome"] == "hedged").sum())), "")]
    items += [("Resolved windows", str(len(rounds)), "")]
    bets_html = ""
    if len(bets):
        rows = "".join(f"<tr><td>{int(r['id'])}</td><td>{esc(r['opened'])}</td><td>{esc(r['kind'])}</td><td>{esc(str(r['coin']).upper())}</td>"
                       f"<td>{esc(r['side'])}</td><td>{float(r['price']):.2f}</td><td>{esc(r['fair'])}</td><td>{n(r['stake'])}</td>"
                       f"<td>{esc(r['winner'])}</td><td class='{cls(float(r['pnl']))}'>{float(r['pnl']):+.2f}</td></tr>"
                       for _, r in bets.tail(60).iloc[::-1].iterrows())
        bets_html = ("<h4>Settled bets</h4><div class='scroll'><table><tr><th>#</th><th>Opened</th><th>Kind</th><th>Coin</th><th>Side</th>"
                     "<th>Price</th><th>Model</th><th>Stake</th><th>Ended</th><th>P/L USD</th></tr>" + rows + "</table></div>")
    lines: list[str] = []
    try:
        c15.research(jdir, cfg, log=lines.append)
    except Exception as e:  # noqa: BLE001
        lines.append(f"research failed: {e}")
    return (f"<h2 id='crypto15'>{esc(f['title'])}</h2><p class='meta'>{esc(f['subtitle'])}</p><p><b>Rules.</b> {esc(f['rules'])}</p>"
            + cards(items) + eq_html + bets_html
            + "<h4>Research: market vs model vs what happened</h4><pre class='small'>" + esc("\n".join(lines)) + "</pre>")


def build(pub: dict) -> Path:
    (OUT / "data").mkdir(parents=True, exist_ok=True)
    for stale in (OUT / "data").glob("*.csv"):   # regenerate the data folder from scratch every time
        stale.unlink()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    topic = pub.get("public_topic", "")
    repo_url = f"https://github.com/{pub.get('github_user', '')}/{pub.get('repo', 'tradingbot')}"
    nav = (" · ".join(f"<a href='#{b['key']}'>{esc(b['title'])}</a>" for b in BOTS)
           + " · <a href='#funds'>Fund rotation bot</a> · <a href='#polymarket'>Polymarket paper bettor</a>"
           + " · <a href='#crypto15'>Crypto 15-minute test</a> · <a href='#copytrade'>Copy-trading test</a>")
    body = [
        f"<h1>{esc(pub.get('site_title', 'Tradingbot'))}</h1>",
        f"<p class='meta'>Seven rule-based bots running on simulated money against real market prices. Every trade, "
        f"position and equity value on this page comes straight from the bots' journals and is regenerated automatically. "
        f"Source code: <a href='{esc(repo_url)}'>{esc(repo_url)}</a>. Last update: {now} (Stockholm time). {nav}</p>",
        "<div class='verdict'><b>Disclaimer.</b> This is a hobby project and a learning exercise. Everything here is paper "
        "trading: no real money is traded by the bots. Nothing on this page is investment advice or a recommendation to buy "
        "or sell anything. Backtest figures are hypothetical and were computed after the fact; they are labelled as such. Past "
        "results, simulated or real, do not predict future results. The author may personally hold some of the assets mentioned. "
        "Trading involves risk of loss; do your own research and never trade money you cannot afford to lose.</div>",
        "<div class='verdict ok'><b>Honest summary so far.</b> In every backtest, simply buying and holding the index beat the "
        "bots. The bots' value, if any, lies in discipline, defined risk and smaller drawdowns, not in higher returns. The fund "
        "rotation bot is the one exception where the trade-off is interesting: roughly index-like returns with less than half "
        "the drawdown in the 2006 to 2026 backtest.</div>",
    ]
    if topic:
        body.append(f"<h2>Follow the signals for free</h2><p>Every signal and paper trade is also pushed to a public "
                    f"<a href='https://ntfy.sh'>ntfy</a> topic. Install the ntfy app (Android, iOS or web), subscribe to the topic "
                    f"<code>{esc(topic)}</code>, and you will get the same notifications as the author, seconds after the bots act. "
                    f"Each message carries the disclaimer above. Note: ntfy topics on the free server are not access-controlled, "
                    f"so treat any message that does not match this page's journals as noise.</p>")
    for b in BOTS:
        body.append(bot_section(b))
    body.append(fund_section(FUND))
    body.append(poly_section(POLY))
    body.append(crypto15_section(CRYPTO15))
    body.append(copytrade_section(COPYTRADE))
    body.append("<h2>Method notes</h2><p class='small'>Signals are always generated on a closed bar and executed on the next bar's "
                "open (or the next close for funds), so no look-ahead. Fees and slippage are deducted on every simulated trade. "
                "Stops are checked against each bar's low in backtests and against the latest price in live paper trading. "
                "Stock data comes from Yahoo Finance (about 15 minutes delayed); crypto data from Binance's public API. "
                "The journals are written to disk as CSV the moment a trade closes and are published here unedited.</p>")
    page = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(pub.get('site_title', 'Tradingbot'))}</title><style>{CSS}h4{{font-size:14px;margin:18px 0 6px}}"
            "code{background:#eef0f3;padding:2px 6px;border-radius:4px}a{color:#1f6feb}</style></head><body><div class='wrap'>"
            + "".join(body) + "</div></body></html>")
    (OUT / "index.html").write_text(page, encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    site = ROOT / "app" / "site"                     # the desktop edition's sales page, published under /dealfinder/
    if site.exists():
        import shutil
        dest = OUT / "dealfinder"
        dest.mkdir(exist_ok=True)
        for f in site.iterdir():
            if f.is_file():
                shutil.copy2(f, dest / f.name)
    (OUT / "README.md").write_text(
        f"# {pub.get('site_title', 'Tradingbot')}\n\nAutomatically published paper-trading journals of seven rule-based trading bots "
        f"(crypto, Swedish stocks, US stocks, fund rotation, Polymarket, crypto 15-minute markets, copy-trading). Live page: https://{pub.get('github_user', 'USER').lower()}.github.io/{pub.get('repo', 'tradingbot')}/\n"
        f"Source code: {repo_url} (branch `main`).\n\n"
        "**Not investment advice. Paper trading only. Past results do not predict future results.**\n\n"
        "The `data/` folder holds the raw journals as CSV (trades, equity, switches), regenerated daily.\n", encoding="utf-8")
    return OUT / "index.html"


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=OUT, text=True, capture_output=True, check=check)


def push(pub: dict) -> None:
    user, repo, branch = pub.get("github_user", "").strip(), pub.get("repo", "tradingbot"), pub.get("branch", "gh-pages")
    if not user:
        print("No github_user in config_publish.json: page built locally, nothing pushed.")
        return
    url = f"https://github.com/{user}/{repo}.git"
    if not (OUT / ".git").exists():
        git("init", "-b", branch)
        git("remote", "add", "origin", url)
    else:
        git("remote", "set-url", "origin", url)
    git("add", "-A")
    ident = ["-c", f"user.name={user}", "-c", f"user.email={user}@users.noreply.github.com"]
    r = subprocess.run(["git", *ident, "commit", "-m", f"Update track record {dt.datetime.now():%Y-%m-%d %H:%M}"],
                       cwd=OUT, text=True, capture_output=True)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        raise RuntimeError(r.stdout + r.stderr)
    r = subprocess.run(["git", "push", "-u", "origin", branch], cwd=OUT, text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("git push failed:\n" + r.stdout + r.stderr)
    print(f"Pushed to {url} ({branch}). Page: https://{user.lower()}.github.io/{repo}/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    pub = json.loads((ROOT / "config_publish.json").read_text(encoding="utf-8"))
    path = build(pub)
    print(f"Built {path}")
    if not args.no_push:
        push(pub)
    return 0


if __name__ == "__main__":
    sys.exit(main())
