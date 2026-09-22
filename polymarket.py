"""The Polymarket paper bettor: scan, run, report, research.

    py polymarket.py scan        list what the scanner would bet on right now (no bets placed)
    py polymarket.py run         paper trading loop: scan, bet simulated money, settle, notify
    py polymarket.py report      HTML report from the journal
    py polymarket.py research    measure the favorite-longshot bias on the bot's own daily snapshots

While running, the bot also checks the real order books of the 200 most traded markets every 5 minutes for the
"YES ask + NO ask < 1" arbitrage that public Polymarket bots advertise, and records how often it exists.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import logging
import os
import sys
import time
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from bot import polymarket as pm
from bot.config import ROOT, fmt_ms, fmt_num, load_config, load_secrets
from bot.notify import Notifier
from bot.report import page, svg_chart


def esc(s) -> str:
    return html.escape(str(s))


def cls(x: float) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def setup_logging(path: Path):
    logger = logging.getLogger("polymarket")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger.info


def cmd_scan(cfg: dict, args) -> int:
    markets = pm.fetch_active_markets(int(cfg.get("scan_limit", 1500)))
    cats = pd.Series([m["category"] for m in markets]).value_counts()
    print("Categories:", ", ".join(f"{k} {v}" for k, v in cats.items()))
    print("\nMulti-outcome arbitrage candidates (negRisk, YES bids sum > 1):")
    arbs = pm.scan_arbitrage(markets, cfg)
    if not arbs:
        print("  none right now (they are rare and taken by fast bots)")
    for c in arbs[:10]:
        print(f"  {c['edge'] * 100:5.1f}%  {c['kind']:11} {len(c['legs'])} legs  {c['title'][:60]}")
    a = cfg.get("arb_check", {})
    print(f"\nSame-market check on the real order books of the {a.get('markets', 200)} most traded markets (YES ask + NO ask < 1?):")
    res = pm.check_same_market_arb(markets, cfg)
    s = res["summary"]
    print(f"  {s['checked']} markets with books, {s['gross_pos']} below 1, {s['gross_1pct']} below 0.99, {s['net_pos']} positive after fees. "
          f"Median sum {s['median_sum']:.3f}, best sum {s['best_sum']:.3f} ({s['best_question'][:50]}).")
    for h in res["hits"][:10]:
        print(f"  {h['net'] * 100:5.2f}% net  sum {h['sum_asks']:.3f}  {h['sets']:8,.0f} sets ({h['usd']:,.0f} USD)  {h['question'][:55]}")
    print("\nFavorite candidates (buy the side priced between "
          f"{cfg['favorites']['min_price']} and {cfg['favorites']['max_price']}, non-sports, resolves within "
          f"{cfg['favorites']['max_days']} days):")
    favs = pm.scan_favorites(markets, cfg, set())
    if not favs:
        print("  none pass the filters right now")
    for c in favs[:20]:
        m = c["market"]
        print(f"  {c['ask']:.3f} {c['name']:>3}  {c['days']:5.1f}d  liq {m['liquidity']:>9,.0f}  {m['category']:8}  {m['question'][:70]}")
    return 0


def cmd_run(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "polymarket")
    jdir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(jdir / "bot.log")
    secrets = load_secrets()
    nt = cfg.get("ntfy", {})
    notifier = Notifier(nt.get("server", "https://ntfy.sh"), secrets.get("ntfy_topic", ""), log,
                        public_topic=nt.get("public_topic", ""), public_url=nt.get("public_url", ""))
    bot = pm.PaperBettor(cfg, jdir, notifier, log)
    st = bot.state
    log("Polymarket paper bettor: scans the public API, bets simulated money on documented edges "
        "(favorites at 90c+ outside sports, and sum-of-prices arbitrage), settles at resolution. No real orders, ever.")
    if st["positions"] or st["bet_counter"]:
        log(f"Resuming: bankroll {bot.money(bot.equity())}, {len(st['positions'])} open bets, {st['bet_counter']} bets so far.")
    else:
        log(f"New bankroll with {bot.money(st['start'])}.")
        notifier.send("Polymarket bot is running (paper)",
                      f"Bankroll {bot.money(st['start'])} simulated money. Scans every {int(cfg.get('poll_seconds', 3600)) // 60} "
                      f"minutes for favorites (90c+, non-sports) and arbitrage.", tags=["robot"])
    poll = int(cfg.get("poll_seconds", 3600))
    check_every = int(cfg.get("arb_check", {}).get("seconds", 300)) if cfg.get("arb_check", {}).get("enabled", True) else poll
    errors = 0
    next_cycle = 0.0
    try:
        while True:
            try:
                if time.time() >= next_cycle:
                    bot.cycle()                       # hourly: full market scan, bets, settlement, snapshot
                    next_cycle = time.time() + poll
                else:
                    bot.arb_check()                   # in between: real order books of the most traded markets
                errors = 0
            except Exception as e:  # noqa: BLE001
                errors += 1
                log(f"Error in cycle ({errors} in a row): {e}")
                log(traceback.format_exc().strip().splitlines()[-1])
                if errors == 5:
                    notifier.send("Polymarket bot has a problem", f"Five errors in a row. Latest: {e}", priority=4, tags=["warning"])
            if args.once:
                break
            time.sleep(max(5.0, min(check_every, next_cycle - time.time())))
    except KeyboardInterrupt:
        bot.save()
        log("Stopped by the user. State is saved, start again with polymarket_start.bat.")
    return 0


def journal_report(cfg: dict, jdir: Path) -> str:
    bets = pm.load_bets(jdir)
    eq = pm.load_equity(jdir)
    start = float(cfg["capital"]["start"])
    body = ["<h1>Journal: Polymarket paper bets</h1>"]
    if len(eq):
        e = eq["equity"].astype(float)
        dd = float((e / e.cummax() - 1).min() * 100)
        items = [("Bankroll", f"{fmt_num(e.iloc[-1])} USD", ""), ("Since start", f"{(e.iloc[-1] / start - 1) * 100:+.2f}%", cls(e.iloc[-1] - start)),
                 ("Max drawdown", f"{dd:.1f}%", "neg"), ("Settled bets", str(len(bets)), "")]
        if len(bets):
            won = int((bets["result"] == "won").sum())
            items += [("Win rate", f"{won / len(bets) * 100:.0f}%", ""), ("Avg per bet", f"{bets['pnl_pct'].mean():+.2f}%", cls(bets["pnl_pct"].mean()))]
        body.append('<div class="cards">' + "".join(
            f'<div class="card"><div class="l">{esc(l)}</div><div class="v {c}">{v}</div></div>' for l, v, c in items) + "</div>")
        n = len(e)
        xl = [(f, fmt_ms(int(eq["ms"].iloc[int(round(f * (n - 1)))]))[:10]) for f in (0, 0.25, 0.5, 0.75, 1.0)] if n > 1 else None
        body.append('<h2>Bankroll</h2><div class="chart">' + svg_chart([("Bankroll", [v / start * 100 for v in e], "#1f6feb")], xl) + "</div>")
    else:
        body.append("<p class='small'>No data yet.</p>")
    ck = jdir / "arb_check.csv"
    if ck.exists() and ck.stat().st_size > 0:
        c = pd.read_csv(ck, encoding="utf-8")
        hits = pd.read_csv(jdir / "arb_hits.csv", encoding="utf-8") if (jdir / "arb_hits.csv").exists() and (jdir / "arb_hits.csv").stat().st_size > 0 else pd.DataFrame()
        body.append("<h2>Same-market arbitrage check</h2><p class='small'>Every few minutes the real order books of the most traded "
                    "Yes/No markets are pulled and YES ask + NO ask compared with 1. This is the \"guaranteed profit\" that public "
                    "Polymarket bots advertise. Counted here: how often it exists at all, and how often anything is left after the taker fee.</p>")
        items = [("Checks", str(len(c)), ""), ("Since", str(c["time"].iloc[0])[:16], ""),
                 ("Markets per check", f"{c['checked'].mean():.0f}", ""),
                 ("Sum below 1", str(int(c["sum_below_1"].sum())), ""), ("Positive after fees", str(int(c["net_positive"].sum())), ""),
                 ("Best sum ever", f"{c['best_sum'].min():.3f}", ""), ("Typical sum", f"{c['median_sum'].median():.3f}", "")]
        body.append('<div class="cards">' + "".join(
            f'<div class="card"><div class="l">{esc(l)}</div><div class="v {k}">{v}</div></div>' for l, v, k in items) + "</div>")
        if len(hits):
            rows = "".join(f"<tr><td>{esc(r['time'])}</td><td>{esc(r['question'])}</td><td>{float(r['yes_ask']):.3f}</td><td>{float(r['no_ask']):.3f}</td>"
                           f"<td>{float(r['sum_asks']):.3f}</td><td>{float(r['net_pct']):+.2f}%</td><td>{float(r['usd']):,.0f}</td><td>{'yes' if int(r['bet_placed']) else 'no'}</td></tr>"
                           for _, r in hits.tail(50).iloc[::-1].iterrows())
            body.append("<div class='scroll'><table><tr><th>Time</th><th>Market</th><th>YES ask</th><th>NO ask</th><th>Sum</th><th>Net after fee</th>"
                        "<th>Size USD</th><th>Paper bet</th></tr>" + rows + "</table></div>")
    import json
    sp = jdir / "state.json"
    if sp.exists():
        st = json.loads(sp.read_text(encoding="utf-8"))
        rows = "".join(f"<tr><td>{p['id']}</td><td>{esc(p['kind'])}</td><td>{esc(p['side_name'])}</td><td>{esc(p['question'])}</td>"
                       f"<td>{p['price']:.3f}</td><td>{fmt_num(p['stake'])}</td><td>{esc(p['end_date'])}</td></tr>" for p in st.get("positions", []))
        body.append("<h2>Open bets</h2>" + ("<div class='scroll'><table><tr><th>#</th><th>Kind</th><th>Side</th><th>Question</th><th>Price</th>"
                    "<th>Stake USD</th><th>Resolves</th></tr>" + rows + "</table></div>" if rows else "<p class='small'>None.</p>"))
    if len(bets):
        rows = "".join(f"<tr><td>{int(r['id'])}</td><td>{esc(r['kind'])}</td><td>{esc(r['side'])}</td><td>{esc(r['question'])}</td>"
                       f"<td>{float(r['price']):.3f}</td><td>{fmt_num(r['stake'])}</td><td class='{cls(float(r['pnl']))}'>{float(r['pnl']):+.2f}</td>"
                       f"<td class='{cls(float(r['pnl_pct']))}'>{float(r['pnl_pct']):+.1f}%</td><td>{esc(r['result'])}</td></tr>"
                       for _, r in bets.iloc[::-1].iterrows())
        body.append("<h2>Settled bets</h2><div class='scroll'><table><tr><th>#</th><th>Kind</th><th>Side</th><th>Question</th><th>Price</th>"
                    "<th>Stake</th><th>P/L USD</th><th>%</th><th>Result</th></tr>" + rows + "</table></div>")
    return page("Journal: Polymarket", "".join(body))


def cmd_report(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "polymarket")
    out = ROOT / "reports" / "report_polymarket.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(journal_report(cfg, jdir), encoding="utf-8")
    print(f"Report saved: {out}")
    if args.open:
        os.startfile(out)  # noqa: S606
    return 0


def cmd_research(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "polymarket")
    pm.research(jdir, log=print)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Polymarket paper bettor.")
    ap.add_argument("command", choices=["scan", "run", "report", "research"])
    ap.add_argument("--config", default="config_polymarket.json")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg = load_config(ROOT / args.config)
    return {"scan": cmd_scan, "run": cmd_run, "report": cmd_report, "research": cmd_research}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
