"""Crypto 15-minute markets watcher: the speed test against Polymarket's Up/Down markets (paper only).

    py crypto15.py scan       show the current windows: Binance move, model probability, Polymarket books
    py crypto15.py run        watch every few seconds, paper-trade the fair-value strategy, replay DipArb, log ticks
    py crypto15.py report     HTML report from the journal
    py crypto15.py research   calibration tables: market vs model vs what actually happened
"""
from __future__ import annotations

import argparse
import html
import math
import os
import sys
import time
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot import crypto15 as c15
from bot.config import ROOT, fmt_ms, fmt_num, load_config, load_secrets
from bot.notify import Notifier
from bot.polymarket import fetch_books
from bot.report import page, svg_chart


def esc(s) -> str:
    return html.escape(str(s))


def cls(x: float) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def setup_logging(path: Path):
    import logging
    logger = logging.getLogger("crypto15")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger.info


def cmd_scan(cfg: dict, args) -> int:
    coins = [c for c in cfg.get("coins", ["btc", "eth"]) if c in c15.SYMBOLS]
    rs = c15.round_start()
    prices = c15.binance_prices([c15.SYMBOLS[c] for c in coins])
    print(f"Window {fmt_ms(rs * 1000, seconds=True)} to {fmt_ms((rs + 900) * 1000, seconds=True)} local time, "
          f"{rs + 900 - time.time():.0f}s left. Fee rate {cfg.get('fee_rate', 0.07)} x p x (1-p) per share for takers.\n")
    for coin in coins:
        m = c15.fetch_round(coin, rs)
        px = prices[c15.SYMBOLS[coin]]
        if not m:
            print(f"{coin.upper()}: no Polymarket market for this window yet.")
            continue
        books = fetch_books([m["up_token"], m["down_token"]])
        up, dn = books.get(m["up_token"], {}), books.get(m["down_token"], {})
        sigma = c15.binance_sigma_per_s(c15.SYMBOLS[coin], int(cfg.get("fair", {}).get("vol_minutes", 60)))
        print(f"{coin.upper()}  {m['question']}")
        print(f"   Binance {px:,.2f} | realised vol {sigma * math.sqrt(900) * 100:.3f}% per 15 min | "
              f"model needs the window's opening price, so it only prices from the next window on")
        print(f"   UP   bid {up.get('bid', 0):.2f}  ask {up.get('ask', 0):.2f} ({up.get('ask_size', 0):,.0f} shares)   "
              f"DOWN bid {dn.get('bid', 0):.2f}  ask {dn.get('ask', 0):.2f} ({dn.get('ask_size', 0):,.0f} shares)   "
              f"UP ask + DOWN ask = {up.get('ask', 0) + dn.get('ask', 0):.2f}")
    return 0


def cmd_run(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "crypto15")
    jdir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(jdir / "bot.log")
    secrets = load_secrets()
    nt = cfg.get("ntfy", {})
    notifier = Notifier(nt.get("server", "https://ntfy.sh"), secrets.get("ntfy_topic", ""), log)
    w = c15.CryptoWatcher(cfg, jdir, notifier, log)
    poll = float(cfg.get("poll_seconds", 5))
    log(f"Crypto 15-minute watcher: {', '.join(c.upper() for c in w.coins)} every {poll:.0f}s. Paper bankroll "
        f"{w.money(w.state['cash'])} for the fair-value strategy, {w.money(w.state['dip_cash'])} for the DipArb replay. "
        f"Nothing is ever sent to Polymarket.")
    if w.state.get("ticks"):
        log("Resuming: " + w.status_line())
    else:
        notifier.send("Crypto 15-minute test is running (paper)",
                      f"Watching Polymarket's {', '.join(c.upper() for c in w.coins)} Up/Down windows every {poll:.0f}s against Binance. "
                      f"Daily summary at {nt.get('daily_report_hour', 21)}:00.", tags=["stopwatch"])
    deadline = time.time() + float(args.seconds) if args.seconds else None
    errors = 0
    last_status = time.time()
    try:
        while True:
            t0 = time.time()
            try:
                w.poll()
                errors = 0
            except Exception as e:  # noqa: BLE001
                errors += 1
                log(f"Error in poll ({errors} in a row): {e}")
                log(traceback.format_exc().strip().splitlines()[-1])
                if errors == 20:
                    notifier.send("Crypto 15-minute test has a problem", f"Twenty errors in a row. Latest: {e}", priority=4, tags=["warning"])
            if time.time() - last_status >= 900:
                last_status = time.time()
                log("Status: " + w.status_line())
            if deadline and time.time() >= deadline:
                break
            time.sleep(max(0.5, poll - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    w.save()
    log("Stopped. " + w.status_line())
    return 0


def journal_report(cfg: dict, jdir: Path) -> str:
    import pandas as pd
    bets, dips, eq, rounds = (c15.load_csv(jdir / n) for n in ("bets.csv", "dips.csv", "equity.csv", "rounds.csv"))
    start = float(cfg["capital"]["start"])
    dip_start = float(cfg.get("diparb", {}).get("start_cash", start))
    body = ["<h1>Journal: crypto 15-minute markets (paper)</h1>",
            "<p class='small'>Polymarket's Bitcoin/Ethereum Up-or-Down windows watched every few seconds against the Binance price. "
            "Two paper strategies: fair value (buy a side when the model probability beats the ask plus fee plus one tick by a margin) "
            "and a replay of the DipArb rule from public Polymarket bots (buy a side that fell 15% in seconds, hedge within 60s at a pair "
            "price of 0.92 or less). Taker fee 0.07 x p x (1-p) per share on every fill.</p>"]
    items = []
    if not eq.empty:
        e, d = eq["equity"].astype(float), eq["dip_equity"].astype(float)
        items += [("Fair-value bankroll", f"{fmt_num(e.iloc[-1])} USD", ""), ("Since start", f"{(e.iloc[-1] / start - 1) * 100:+.2f}%", cls(e.iloc[-1] - start)),
                  ("DipArb bankroll", f"{fmt_num(d.iloc[-1])} USD", ""), ("Since start", f"{(d.iloc[-1] / dip_start - 1) * 100:+.2f}%", cls(d.iloc[-1] - dip_start))]
    if not bets.empty:
        won = int((bets["result"] == "won").sum())
        items += [("Settled bets", str(len(bets)), ""), ("Win rate", f"{won / len(bets) * 100:.0f}%", ""),
                  ("Avg per bet", f"{bets['pnl_pct'].mean():+.1f}%", cls(bets["pnl_pct"].mean()))]
    if not dips.empty:
        items += [("Dips seen", str(len(dips)), ""), ("Hedged", str(int((dips["outcome"] == "hedged").sum())), "")]
    items += [("Resolved windows", str(len(rounds)), "")]
    body.append('<div class="cards">' + "".join(f'<div class="card"><div class="l">{esc(l)}</div><div class="v {c}">{v}</div></div>' for l, v, c in items) + "</div>")
    if not eq.empty:
        n = len(eq)
        xl = [(f, fmt_ms(int(eq["ms"].iloc[int(round(f * (n - 1)))]))[:16]) for f in (0, 0.25, 0.5, 0.75, 1.0)] if n > 1 else None
        body.append("<h2>Bankrolls, % of start</h2><div class='chart'>" + svg_chart(
            [("Fair value", [v / start * 100 for v in eq["equity"].astype(float)], "#1f6feb"),
             ("DipArb replay", [v / dip_start * 100 for v in eq["dip_equity"].astype(float)], "#d29922")], xl) + "</div>")
    if not bets.empty:
        rows = "".join(f"<tr><td>{int(r['id'])}</td><td>{esc(r['opened'])}</td><td>{esc(r['kind'])}</td><td>{esc(str(r['coin']).upper())}</td>"
                       f"<td>{esc(r['side'])}</td><td>{float(r['price']):.2f}</td><td>{esc(r['fair'])}</td><td>{fmt_num(r['stake'])}</td>"
                       f"<td>{esc(r['winner'])}</td><td class='{cls(float(r['pnl']))}'>{float(r['pnl']):+.2f}</td><td>{esc(r['resolved_by'])}</td></tr>"
                       for _, r in bets.tail(80).iloc[::-1].iterrows())
        body.append("<h2>Settled bets</h2><div class='scroll'><table><tr><th>#</th><th>Opened</th><th>Kind</th><th>Coin</th><th>Side</th>"
                    "<th>Price</th><th>Model</th><th>Stake</th><th>Ended</th><th>P/L USD</th><th>Resolved by</th></tr>" + rows + "</table></div>")
    if not dips.empty:
        rows = "".join(f"<tr><td>{esc(r['time'])}</td><td>{esc(str(r['coin']).upper())}</td><td>{esc(r['side'])}</td><td>{float(r['from_ask']):.2f} → {float(r['to_ask']):.2f}</td>"
                       f"<td>{float(r['t_left']):.0f}s</td><td>{esc(r['min_sum'])}</td><td>{esc(r['outcome'])}</td><td>{esc(r['pnl'])}</td></tr>"
                       for _, r in dips.tail(80).iloc[::-1].iterrows())
        body.append("<h2>DipArb replay</h2><div class='scroll'><table><tr><th>Time</th><th>Coin</th><th>Side</th><th>Ask drop</th><th>Left</th>"
                    "<th>Best pair price</th><th>Outcome</th><th>P/L USD</th></tr>" + rows + "</table></div>")
    lines: list[str] = []
    try:
        c15.research(jdir, cfg, log=lines.append)
    except Exception as e:  # noqa: BLE001
        lines.append(f"research failed: {e}")
    body.append("<h2>Research: is anyone right?</h2><pre class='small'>" + esc("\n".join(lines)) + "</pre>")
    return page("Journal: crypto 15-minute markets", "".join(body))


def cmd_report(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "crypto15")
    out = ROOT / "reports" / "report_crypto15.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(journal_report(cfg, jdir), encoding="utf-8")
    print(f"Report saved: {out}")
    if args.open:
        os.startfile(out)  # noqa: S606
    return 0


def cmd_research(cfg: dict, args) -> int:
    c15.research(ROOT / "journal" / cfg.get("journal", "crypto15"), cfg, log=print)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Crypto 15-minute markets watcher (paper).")
    ap.add_argument("command", choices=["scan", "run", "report", "research"])
    ap.add_argument("--config", default="config_crypto15.json")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--seconds", type=float, default=0, help="run: stop after this many seconds (for tests)")
    args = ap.parse_args()
    cfg = load_config(ROOT / args.config)
    return {"scan": cmd_scan, "run": cmd_run, "report": cmd_report, "research": cmd_research}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
