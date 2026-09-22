"""Copy-trading measurement: follow Polymarket leaderboard wallets with paper money.

    py copytrade.py wallets    judge the leaderboard wallets and show who would be followed right now
    py copytrade.py run        follow them: copy buys and sells on paper, settle at resolution, log every signal
    py copytrade.py report     HTML report from the journal
"""
from __future__ import annotations

import argparse
import html
import os
import sys
import time
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot import copytrade as ct
from bot.config import ROOT, fmt_ms, fmt_num, load_config, load_secrets
from bot.notify import Notifier
from bot.report import page, svg_chart


def esc(s) -> str:
    return html.escape(str(s))


def cls(x: float) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def setup_logging(path: Path):
    import logging
    logger = logging.getLogger("copytrade")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger.info


def cmd_wallets(cfg: dict, args) -> int:
    followed, judged = ct.select_wallets(cfg, print)
    print(f"\n{len(judged)} wallets judged, {len(followed)} pass the gate:\n")
    print(f"{'name':22} {'board':16} {'period P/L':>12} {'closed':>6} {'WR':>5} {'PF':>6} {'max1':>5} {'updown':>6} {'idle h':>7}  verdict")
    for j in sorted(judged, key=lambda x: (not x["passed"], -x["pnl"])):
        print(f"{j['name'][:22]:22} {j['board']:16} {j['pnl']:>12,.0f} {j['closed_n']:>6} {j['win_rate'] * 100:>4.0f}% {j['profit_factor']:>6.2f} "
              f"{j['max_single_share'] * 100:>4.0f}% {j['updown_share'] * 100:>5.0f}% {j['last_trade_h']:>7.0f}  {'FOLLOW' if j['passed'] else j['reason'][:70]}")
    return 0


def cmd_run(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "copytrade")
    jdir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(jdir / "bot.log")
    secrets = load_secrets()
    nt = cfg.get("ntfy", {})
    notifier = Notifier(nt.get("server", "https://ntfy.sh"), secrets.get("ntfy_topic", ""), log)
    bot = ct.CopyTrader(cfg, jdir, notifier, log)
    poll = float(cfg.get("poll_seconds", 60))
    log(f"Copy-trading test: follows leaderboard wallets that pass the public bots' gate, copies their buys and sells on paper "
        f"with {bot.money(bot.state['start'])}, polling every {poll:.0f}s. Nothing is ever sent to Polymarket.")
    if bot.state.get("signals"):
        log("Resuming: " + bot.status_line())
    else:
        notifier.send("Copy-trading test is running (paper)",
                      f"Following Polymarket leaderboard wallets with {bot.money(bot.state['start'])} simulated. Daily summary at "
                      f"{nt.get('daily_report_hour', 21)}:00.", tags=["busts_in_silhouette"])
    deadline = time.time() + float(args.seconds) if args.seconds else None
    errors = 0
    try:
        while True:
            t0 = time.time()
            try:
                bot.cycle()
                errors = 0
            except Exception as e:  # noqa: BLE001
                errors += 1
                log(f"Error in cycle ({errors} in a row): {e}")
                log(traceback.format_exc().strip().splitlines()[-1])
                if errors == 10:
                    notifier.send("Copy-trading test has a problem", f"Ten errors in a row. Latest: {e}", priority=4, tags=["warning"])
            if deadline and time.time() >= deadline:
                break
            time.sleep(max(5.0, poll - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    bot.save()
    log("Stopped. " + bot.status_line())
    return 0


def journal_report(cfg: dict, jdir: Path) -> str:
    import json
    import pandas as pd
    bets, signals, eq, wallets = (ct.load_csv(jdir / n) for n in ("bets.csv", "signals.csv", "equity.csv", "wallets.csv"))
    start = float(cfg["capital"]["start"])
    body = ["<h1>Journal: copy-trading test (paper)</h1>",
            "<p class='small'>Follows Polymarket leaderboard wallets that pass the gate advertised by public copy-trading bots "
            "(60%+ win rate, profit factor 1.5+, 30+ closed positions, no single position above 30% of profit) and copies their "
            "buys and sells on paper at the live best ask/bid plus one tick and the taker fee. Every signal is logged with how late "
            "it was seen and how far the price had moved.</p>"]
    items = []
    if not eq.empty:
        e = eq["equity"].astype(float)
        items += [("Bankroll", f"{fmt_num(e.iloc[-1])} USD", ""), ("Since start", f"{(e.iloc[-1] / start - 1) * 100:+.2f}%", cls(e.iloc[-1] - start))]
    if not bets.empty:
        won = int((bets["result"] == "won").sum())
        items += [("Closed copies", str(len(bets)), ""), ("Win rate", f"{won / len(bets) * 100:.0f}%", ""),
                  ("Avg per copy", f"{bets['pnl_pct'].mean():+.1f}%", cls(bets["pnl_pct"].mean()))]
    if not signals.empty:
        buys = signals[signals["side"] == "BUY"]
        copied = signals[signals["action"] == "copied"]
        items += [("Signals seen", str(len(signals)), ""), ("Buys copied", f"{len(copied)} of {len(buys)}", ""),
                  ("Median delay", f"{signals['delay_s'].median() / 60:.0f} min", "")]
        prem = pd.to_numeric(copied["premium_pct"], errors="coerce").dropna()
        if len(prem):
            items += [("Avg price premium", f"{prem.mean():+.1f}%", cls(-prem.mean()))]
    body.append('<div class="cards">' + "".join(f'<div class="card"><div class="l">{esc(l)}</div><div class="v {c}">{v}</div></div>' for l, v, c in items) + "</div>")
    if not eq.empty:
        n = len(eq)
        xl = [(f, fmt_ms(int(eq["ms"].iloc[int(round(f * (n - 1)))]))[:16]) for f in (0, 0.25, 0.5, 0.75, 1.0)] if n > 1 else None
        body.append("<h2>Bankroll, % of start</h2><div class='chart'>" + svg_chart([("Copy-trading", [v / start * 100 for v in eq["equity"].astype(float)], "#1f6feb")], xl) + "</div>")
    sp = jdir / "state.json"
    if sp.exists():
        st = json.loads(sp.read_text(encoding="utf-8"))
        rows = "".join(f"<tr><td>{esc(v['name'])}</td><td>{esc(v.get('board', ''))}</td><td>{float(v.get('pnl', 0)):,.0f}</td><td>{float(v.get('win_rate', 0)) * 100:.0f}%</td>"
                       f"<td>{float(v.get('profit_factor', 0)):.2f}</td><td>{esc(v.get('closed_n', ''))}</td><td>{'retired' if v.get('retired') else 'active'}</td></tr>"
                       for v in st.get("wallets", {}).values())
        body.append("<h2>Wallets followed</h2><div class='scroll'><table><tr><th>Name</th><th>Board</th><th>Period P/L USD</th><th>Win rate</th>"
                    "<th>Profit factor</th><th>Closed</th><th>Status</th></tr>" + rows + "</table></div>")
        rows = "".join(f"<tr><td>{p['id']}</td><td>{esc(p['name'])}</td><td>{esc(p['outcome'])}</td><td>{esc(p['question'])}</td><td>{float(p['their_price']):.2f}</td>"
                       f"<td>{float(p['price']):.2f}</td><td>{float(p.get('mark', p['price'])):.2f}</td><td>{fmt_num(p['stake'])}</td><td>{p['delay_s'] / 60:.0f} min</td><td>{esc(p.get('end_date', ''))}</td></tr>"
                       for p in st.get("positions", []))
        body.append("<h2>Open copies</h2>" + ("<div class='scroll'><table><tr><th>#</th><th>Wallet</th><th>Side</th><th>Market</th><th>Their price</th>"
                    "<th>Our price</th><th>Now</th><th>Stake</th><th>Delay</th><th>Resolves</th></tr>" + rows + "</table></div>" if rows else "<p class='small'>None.</p>"))
    if not bets.empty:
        rows = "".join(f"<tr><td>{int(r['id'])}</td><td>{esc(r['name'])}</td><td>{esc(r['outcome'])}</td><td>{esc(r['question'])}</td><td>{float(r['their_price']):.2f}</td>"
                       f"<td>{float(r['price']):.2f}</td><td>{esc(r['exit'])}</td><td>{float(r['exit_price']):.2f}</td><td class='{cls(float(r['pnl']))}'>{float(r['pnl']):+.2f}</td></tr>"
                       for _, r in bets.tail(80).iloc[::-1].iterrows())
        body.append("<h2>Closed copies</h2><div class='scroll'><table><tr><th>#</th><th>Wallet</th><th>Side</th><th>Market</th><th>Their price</th>"
                    "<th>Our price</th><th>Exit</th><th>Exit price</th><th>P/L USD</th></tr>" + rows + "</table></div>")
    if not signals.empty:
        reasons = signals[signals["action"] != "copied"]["reason"].fillna("").replace("", "(none)").value_counts().head(12)
        rows = "".join(f"<tr><td>{esc(k)}</td><td>{v}</td></tr>" for k, v in reasons.items())
        body.append("<h2>Why signals were not copied</h2><div class='scroll'><table><tr><th>Reason</th><th>Count</th></tr>" + rows + "</table></div>")
    return page("Journal: copy-trading", "".join(body))


def cmd_report(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "copytrade")
    out = ROOT / "reports" / "report_copytrade.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(journal_report(cfg, jdir), encoding="utf-8")
    print(f"Report saved: {out}")
    if args.open:
        os.startfile(out)  # noqa: S606
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Copy-trading measurement (paper).")
    ap.add_argument("command", choices=["wallets", "run", "report"])
    ap.add_argument("--config", default="config_copytrade.json")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--seconds", type=float, default=0, help="run: stop after this many seconds (for tests)")
    args = ap.parse_args()
    cfg = load_config(ROOT / args.config)
    return {"wallets": cmd_wallets, "run": cmd_run, "report": cmd_report}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
