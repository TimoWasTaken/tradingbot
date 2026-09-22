"""The fund rotation bot: backtest, paper trading and report.

    py rotation.py backtest        tests the rule on ~20 years of history and writes an HTML report
    py rotation.py run             runs paper trading (one decision per month, push on every decision)
    py rotation.py report          HTML report from the journal
    Add --open to open the report in the browser, --config for another configuration file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from bot import rotation as rot
from bot.config import ROOT, fmt_ms, fmt_num, load_config, load_secrets
from bot.notify import Notifier
from bot.report import page, svg_chart


def esc(s) -> str:
    return html.escape(str(s))


def setup_logging(path: Path):
    logger = logging.getLogger("rotation")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger.info


# ---------------- report parts ----------------

def cards(items) -> str:
    return '<div class="cards">' + "".join(
        f'<div class="card"><div class="l">{esc(l)}</div><div class="v {c}">{v}</div></div>' for l, v, c in items) + "</div>"


def cls(x: float) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def chart(eq: pd.Series, benches: dict[str, pd.Series], start_cap: float) -> str:
    series = [("Fund rotation", [v / start_cap * 100 for v in eq.to_numpy()], "#1f6feb")]
    colors = ["#e36209", "#8250df", "#1a7f37"]
    for k, (name, s) in enumerate(benches.items()):
        series.append((f"Buy & hold {name}", [v / start_cap * 100 for v in s.to_numpy()], colors[k % 3]))
    n = len(eq)
    xl = [(f, fmt_ms(int(eq.index[int(round(f * (n - 1)))]))[:10]) for f in (0, 0.25, 0.5, 0.75, 1.0)] if n > 1 else None
    return ('<div class="chart">' + svg_chart(series, xl) +
            '<p class="small">Start = 100. Linear scale, so early years look flatter than they were.</p></div>')


def year_table(rows: dict[str, dict]) -> str:
    years = sorted({y for d in rows.values() for y in d})
    head = "<tr><th>Year</th>" + "".join(f"<th>{esc(k)}</th>" for k in rows) + "</tr>"
    body = ""
    for y in years:
        body += f"<tr><td>{y}</td>" + "".join(
            f'<td class="{cls(d.get(y, 0))}">{d[y]:+.1f}%</td>' if y in d else "<td>–</td>" for d in rows.values()) + "</tr>"
    return '<div class="scroll"><table>' + head + body + "</table></div>"


def alloc_table(alloc, names: dict, funds: dict, limit: int = 36) -> str:
    rows = ""
    for ms, chosen in list(alloc)[-limit:][::-1]:
        rows += (f"<tr><td>{esc(fmt_ms(ms)[:10])}</td><td>{esc(', '.join(names.get(t, t) for t in chosen))}</td>"
                 f"<td>{esc(', '.join(funds.get(t, t) for t in chosen))}</td></tr>")
    return ('<div class="scroll"><table><tr><th>Month end</th><th>Assets (proxy ETFs)</th><th>Corresponding Avanza funds</th></tr>'
            + rows + "</table></div>")


def verdict(stats: dict, bench_stats: dict[str, dict]) -> tuple[str, str]:
    best = max(bench_stats.items(), key=lambda kv: kv[1]["cagr_pct"])
    txt = (f"{stats['cagr_pct']:+.1f}% per year on average over {stats['years']:.0f} years, max drawdown {stats['max_dd_pct']:.0f}%, "
           f"worst year {stats['worst_year_pct']:+.0f}%. Best comparison, buy & hold {best[0]}: "
           f"{best[1]['cagr_pct']:+.1f}% per year with max drawdown {best[1]['max_dd_pct']:.0f}%.")
    if stats["cagr_pct"] >= best[1]["cagr_pct"] - 1 and stats["max_dd_pct"] > best[1]["max_dd_pct"] + 5:
        return "ok", txt + " Roughly the same return as the index with clearly smaller drawdowns. That is what the rule is meant to deliver."
    if stats["max_dd_pct"] > best[1]["max_dd_pct"] + 5:
        return "warn", txt + " Lower return than the index, but much smaller drawdowns. A trade-off, not a free lunch."
    return "bad", txt + " Neither higher return nor smaller drawdown than the index. Then simply owning the index fund is better."


def backtest_report(cfg: dict, res: dict, close: pd.DataFrame, variants: list[dict]) -> str:
    start_cap = res["start_cap"]
    eq = res["equity"]
    stats = rot.stats_from_equity(eq, start_cap)
    benches = {name: rot.benchmark_equity(close, t, eq, start_cap) for t, name in cfg.get("benchmarks", {}).items()}
    bstats = {name: rot.stats_from_equity(s, start_cap) for name, s in benches.items()}
    k, text = verdict(stats, bstats)
    names = cfg.get("ticker_names", {})
    funds = cfg.get("fund_names", {})
    items = [
        ("Total", f"{stats['total_pct']:+.0f}%", cls(stats["total_pct"])),
        ("Per year (CAGR)", f"{stats['cagr_pct']:+.1f}%", cls(stats["cagr_pct"])),
        ("End value", f"{fmt_num(eq.iloc[-1], 0)} SEK", ""),
        ("Max drawdown", f"{stats['max_dd_pct']:.0f}%", "neg"),
        ("Worst year", f"{stats['worst_year_pct']:+.0f}%", "neg"),
        ("Best year", f"{stats['best_year_pct']:+.0f}%", "pos"),
        ("Switches", str(len(res["switches"])), ""),
        ("Switches per year", f"{len(res['switches']) / max(stats['years'], 1e-9):.1f}", ""),
    ]
    safe = cfg["safe"]
    months_safe = sum(1 for _ms, ch in res["alloc"] if safe in ch)
    items.append(("Months partly in bonds", f"{months_safe} of {len(res['alloc'])}", ""))
    for name, bs in bstats.items():
        items.append((f"Buy & hold {name}", f"{bs['cagr_pct']:+.1f}%/yr, DD {bs['max_dd_pct']:.0f}%", ""))
    vrows = "".join(
        f"<tr><td>{esc(v['label'])}</td><td class='{cls(v['stats']['cagr_pct'])}'>{v['stats']['cagr_pct']:+.1f}%</td>"
        f"<td class='neg'>{v['stats']['max_dd_pct']:.0f}%</td><td class='neg'>{v['stats']['worst_year_pct']:+.0f}%</td>"
        f"<td>{v['n_switch']}</td></tr>" for v in variants)
    per_year = {"Fund rotation": stats["per_year"]}
    for name, bs in bstats.items():
        per_year[name] = bs["per_year"]
    sw_rows = "".join(
        f"<tr><td>{esc(fmt_ms(s['ms'])[:10])}</td><td>{esc(', '.join(names.get(t, t) for t in s['from']) or '–')}</td>"
        f"<td>{esc(', '.join(names.get(t, t) for t in s['to']))}</td><td>{fmt_num(s['value'], 0)} SEK</td></tr>"
        for s in res["switches"][-40:][::-1])
    body = [
        "<h1>Backtest: fund rotation</h1>",
        f"<div class='meta'>Period {esc(fmt_ms(int(eq.index[0]))[:10])} to {esc(fmt_ms(int(eq.index[-1]))[:10])}. "
        f"Starting capital {fmt_num(start_cap, 0)} SEK. Universe: {esc(', '.join(names.get(t, t) for t in cfg['universe']))}. "
        f"Safe asset: {esc(names.get(safe, safe))}. Holds the top {cfg['top_n']} by average 3/6/12-month return, "
        f"switching on the last trading day of the month. Fund fee {cfg.get('fund_fee_pct_per_year', 0.3)}% per year and "
        f"{cfg.get('slippage_pct', 0.1)}% price drift per switch deducted. SEK/USD currency moves are not included. "
        f"Generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}.</div>",
        f"<div class='verdict {k}'><b>Verdict:</b> {esc(text)}</div>",
        cards(items),
        "<h2>Equity curve</h2>", chart(eq, benches, start_cap),
        "<h2>Variants of the rule</h2>",
        "<p class='small'>Same data, other settings. The chosen one is the configuration file's.</p>",
        "<div class='scroll'><table><tr><th>Variant</th><th>Per year</th><th>Max drawdown</th><th>Worst year</th><th>Switches</th></tr>"
        + vrows + "</table></div>",
        "<h2>Year by year</h2>", year_table(per_year),
        "<h2>What the bot held, last three years</h2>", alloc_table(res["alloc"], names, funds),
        "<h2>Latest switches</h2>",
        "<div class='scroll'><table><tr><th>Date</th><th>From</th><th>To</th><th>Value</th></tr>" + sw_rows + "</table></div>",
        "<h2>Glossary</h2><dl>"
        "<dt>Momentum</dt><dd>What has done well over the past year tends to keep doing well for a few more months. "
        "One of the best-documented effects in financial markets.</dd>"
        "<dt>Safe asset</dt><dd>Short-term bonds. If an asset does not beat bonds over 3 to 12 months the bot holds bonds instead. "
        "This is what cuts off the big drawdowns.</dd>"
        "<dt>CAGR</dt><dd>Compound annual growth rate: average return per year with compounding.</dd>"
        "<dt>Max drawdown</dt><dd>The largest fall from a peak to a subsequent trough. Tells you how much it hurts along the way.</dd></dl>",
    ]
    return page("Backtest: fund rotation", "".join(body))


def journal_report(cfg: dict, jdir: Path) -> str:
    ep = jdir / "equity.csv"
    sp = jdir / "switches.csv"
    names = cfg.get("fund_names", {})
    start = float(cfg["capital"]["start"])
    body = ["<h1>Journal: fund rotation (paper)</h1>"]
    if ep.exists() and ep.stat().st_size > 0:
        k = pd.read_csv(ep)
        eq = pd.Series(k["equity"].to_numpy(dtype=float), index=k["ms"].to_numpy())
        st = rot.stats_from_equity(eq, start)
        body.append(cards([("Value", f"{fmt_num(eq.iloc[-1], 0)} SEK", ""),
                           ("Since start", f"{(eq.iloc[-1] / start - 1) * 100:+.1f}%", cls(eq.iloc[-1] - start)),
                           ("Max drawdown", f"{st['max_dd_pct']:.1f}%", "neg"),
                           ("Days of data", str(len(eq)), "")]))
        body.append("<h2>Equity curve</h2>" + chart(eq, {}, start))
    else:
        body.append("<p class='small'>No valuation yet.</p>")
    stp = jdir / "state.json"
    if stp.exists():
        st = json.loads(stp.read_text(encoding="utf-8"))
        cur = ", ".join(names.get(t, t) for t in st.get("current", [])) or "nothing yet"
        body.append(f"<h2>Holdings</h2><p>{esc(cur)}</p>")
        if st.get("pending"):
            body.append("<p class='small'>Pending switch to: " +
                        esc(", ".join(names.get(t, t) for t in st["pending"]["chosen"])) + "</p>")
    if sp.exists() and sp.stat().st_size > 0:
        b = pd.read_csv(sp)
        rows = "".join(f"<tr><td>{esc(r['time'])}</td><td>{esc(r['portfolio'])}</td><td>{fmt_num(r['value'], 0)} SEK</td></tr>"
                       for _, r in b.iloc[::-1].iterrows())
        body.append("<h2>Switches</h2><div class='scroll'><table><tr><th>Time</th><th>Portfolio</th><th>Value</th></tr>" + rows + "</table></div>")
    return page("Journal: fund rotation", "".join(body))


# ---------------- commands ----------------

def cmd_backtest(cfg: dict, args) -> int:
    days = args.days or int(cfg.get("backtest", {}).get("days", 7300))
    close = rot.load_close_history(cfg, days, log=print)
    names = cfg.get("ticker_names", {})
    print(f"\nData: {len(close)} trading days, {fmt_ms(int(close.index[0]))[:10]} to {fmt_ms(int(close.index[-1]))[:10]}, "
          f"{', '.join(names.get(t, t) for t in close.columns)}\n")
    variants = []
    for top_n in (1, 2, 3):
        for trend in (0, 200):
            c = dict(cfg)
            c["top_n"] = top_n
            c["trend_sma"] = trend
            label = f"Top {top_n}" + (", price must be above its 200-day average" if trend else "")
            r = rot.run_backtest(c, close, label)
            s = rot.stats_from_equity(r["equity"], r["start_cap"])
            variants.append({"label": label, "stats": s, "n_switch": len(r["switches"])})
            print(f"{label:48} {s['cagr_pct']:+6.1f}%/yr  drawdown {s['max_dd_pct']:6.1f}%  "
                  f"worst year {s['worst_year_pct']:+6.1f}%  switches {len(r['switches'])}")
    res = rot.run_backtest(cfg, close, "chosen")
    stats = rot.stats_from_equity(res["equity"], res["start_cap"])
    print(f"\nChosen setting (top {cfg['top_n']}{', trend filter' if cfg.get('trend_sma') else ''}): "
          f"{stats['cagr_pct']:+.1f}%/yr, max drawdown {stats['max_dd_pct']:.1f}%, {len(res['switches'])} switches.")
    for t, name in cfg.get("benchmarks", {}).items():
        bs = rot.stats_from_equity(rot.benchmark_equity(close, t, res["equity"], res["start_cap"]), res["start_cap"])
        print(f"Buy & hold {name}: {bs['cagr_pct']:+.1f}%/yr, max drawdown {bs['max_dd_pct']:.1f}%")
    html_text = backtest_report(cfg, res, close, variants)
    rep = ROOT / "reports"
    rep.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    path = rep / f"backtest_funds_{stamp}.html"
    path.write_text(html_text, encoding="utf-8")
    (rep / "latest_backtest_funds.html").write_text(html_text, encoding="utf-8")
    print(f"\nReport saved: {path}")
    if args.open:
        os.startfile(path)  # noqa: S606
    return 0


def cmd_run(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "funds")
    jdir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(jdir / "bot.log")
    secrets = load_secrets()
    nt = cfg.get("ntfy", {})
    notifier = Notifier(nt.get("server", "https://ntfy.sh"), secrets.get("ntfy_topic", ""), log,
                        public_topic=nt.get("public_topic", ""), public_url=nt.get("public_url", ""))
    bot = rot.RotationPaper(cfg, jdir, notifier, log)
    names = cfg.get("fund_names", {})
    log("Fund rotation, paper trading: one decision per month on the last trading day (about 22:05 Stockholm time), "
        "the switch is booked at the next day's close.")
    log(f"Universe: {', '.join(names.get(t, t) for t in cfg['universe'])}. Safe asset: {names.get(cfg['safe'], cfg['safe'])}. "
        f"Holds the top {cfg['top_n']}. Push: {'on (' + notifier.topic + ')' if notifier.enabled else 'off'}"
        f"{', public topic ' + notifier.public_topic if notifier.public_enabled else ''}.")
    st = bot.state
    if st.get("current") or st.get("pending"):
        log(f"Resuming: holdings {', '.join(names.get(t, t) for t in st['current']) or 'none'}, "
            f"{'a pending switch' if st.get('pending') else 'no pending switch'}, {st.get('switches', 0)} switches so far.")
    else:
        log(f"New account with {fmt_num(st['start'], 0)} SEK. The starting portfolio is decided in the first cycle.")
        notifier.send("Fund rotation bot is running (paper trading)",
                      f"Rotates between {len(cfg['universe'])} broad funds by momentum, one decision per month. "
                      f"Equity {fmt_num(st['start'], 0)} SEK simulated money. The starting portfolio comes in the next notification.",
                      tags=["robot"])
    poll = int(cfg.get("poll_seconds", 3600))
    errors = 0
    try:
        while True:
            try:
                bot.cycle()
                errors = 0
            except Exception as e:  # noqa: BLE001
                errors += 1
                log(f"Error in cycle ({errors} in a row): {e}")
                log(traceback.format_exc().strip().splitlines()[-1])
                if errors == 5:
                    notifier.send("Fund rotation bot has a problem", f"Five errors in a row. Latest: {e}",
                                  priority=4, tags=["warning"])
            if args.once:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        bot.save()
        log("Stopped by the user. State is saved, start again with funds_start.bat.")
    return 0


def cmd_report(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "funds")
    out = ROOT / "reports" / "report_funds.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(journal_report(cfg, jdir), encoding="utf-8")
    print(f"Report saved: {out}")
    if args.open:
        os.startfile(out)  # noqa: S606
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="The fund rotation bot.")
    ap.add_argument("command", choices=["backtest", "run", "report"])
    ap.add_argument("--config", default="config_funds.json")
    ap.add_argument("--days", type=int)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg = load_config(ROOT / args.config)
    return {"backtest": cmd_backtest, "run": cmd_run, "report": cmd_report}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
