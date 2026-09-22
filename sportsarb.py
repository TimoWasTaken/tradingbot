"""Sports arbitrage measurement: how much sure-bet money is on the table at Swedish-licensed bookmakers?

    py sportsarb.py scan       one scan now, prints the result (costs one API credit per sport)
    py sportsarb.py run        scans at the hours in config_sportsarb.json, pushes a note on every new arbitrage
    py sportsarb.py report     summary of everything measured so far (HTML + console)

Needs a free key from https://the-odds-api.com in secrets.json as "odds_api_key". Nothing is ever bet.
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

from bot import sportsarb as sa
from bot.config import ROOT, load_config, load_secrets
from bot.notify import Notifier
from bot.report import page


def esc(s) -> str:
    return html.escape(str(s))


def setup_logging(path: Path):
    logger = logging.getLogger("sportsarb")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger.info


def make(cfg: dict, log):
    secrets = load_secrets()
    api = sa.OddsAPI(secrets.get("odds_api_key", ""))
    nt = cfg.get("ntfy", {})
    notifier = Notifier(nt.get("server", "https://ntfy.sh"), secrets.get("ntfy_topic", ""), log,
                        public_topic=nt.get("public_topic", ""), public_url=nt.get("public_url", ""))
    jdir = ROOT / "journal" / cfg.get("journal", "sportsarb")
    return sa.ArbScanner(cfg, api, jdir, notifier, log), jdir


def print_result(res: dict, scanner: sa.ArbScanner) -> None:
    for label, title in (("arbs_se", "Swedish-licensed bookmakers only"), ("arbs_all", "all bookmakers incl. Pinnacle/exchange")):
        print(f"\nArbitrages, {title}: {len(res[label])}")
        for a in sorted(res[label], key=lambda x: -x["profit_pct"])[:15]:
            print(f"  {a['profit_pct']:5.2f}%  {a['sport']:32} {a['event'][:40]:40} {a['legs']}")
    print(f"\nAPI credits left this month: {scanner.api.remaining}")


def cmd_scan(cfg: dict, args) -> int:
    try:
        scanner, _ = make(cfg, print)
    except ValueError as e:
        print(e)
        return 1
    res = scanner.scan(notify=False)
    print_result(res, scanner)
    return 0


def cmd_run(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "sportsarb")
    jdir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(jdir / "bot.log")
    try:
        scanner, _ = make(cfg, log)
    except ValueError as e:
        log(str(e))
        return 1
    hours = [int(h) for h in cfg.get("scan_hours", [9, 18])]
    log(f"Sports arbitrage measurement: scanning {len(cfg['sports'])} sports at {', '.join(f'{h:02d}:00' for h in hours)} "
        f"every day, {len(scanner.licensed)} Swedish-licensed bookmakers ({', '.join(scanner.book(b) for b in scanner.licensed)}) "
        f"plus {len(scanner.all_books) - len(scanner.licensed)} reference books. Nothing is bet.")
    last_slot = ""
    if args.once:
        res = scanner.scan(notify=True)
        print_result(res, scanner)
        return 0
    try:
        while True:
            now = dt.datetime.now()
            slot = f"{now:%Y-%m-%d}-{now.hour}"
            if now.hour in hours and slot != last_slot:
                last_slot = slot
                try:
                    scanner.scan(notify=True)
                except Exception as e:  # noqa: BLE001
                    log(f"Scan failed: {e}")
                    log(traceback.format_exc().strip().splitlines()[-1])
            time.sleep(120)
    except KeyboardInterrupt:
        log("Stopped by the user.")
    return 0


def cmd_report(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "sportsarb")
    capital = float(cfg.get("capital_sek", 10000))
    s = sa.summarize(jdir, capital)
    if not s["scans"]:
        print("No scans yet.")
        return 0
    print(f"{s['scans']} scans over {s['days']:.1f} days, {s['events']} event snapshots.")
    if s.get("median_margin_se_pct") is not None:
        print(f"Median bookmaker margin at the best Swedish-licensed odds: {s['median_margin_se_pct']:.2f}% "
              f"(all books: {s['median_margin_all_pct']:.2f}%). Events within 1% of an arbitrage: {s['share_near_arb_pct']:.1f}%.")
    for label, title in (("se", "Swedish-licensed only"), ("all", "all bookmakers")):
        print(f"\n{title}: {s[f'arbs_{label}']} arbitrages (one per event and day), average profit "
              f"{s[f'avg_profit_{label}_pct']:.2f}%, best {s[f'max_profit_{label}_pct']:.2f}%. "
              f"If you had caught every one with {capital:,.0f} SEK: about {s[f'sek_per_week_{label}']:,.0f} SEK per week.")
        if s[f"books_{label}"]:
            print("  bookmakers involved: " + ", ".join(f"{k} {v}" for k, v in list(s[f"books_{label}"].items())[:8]))
    arbs = sa.load(jdir, "arbs.csv", sa.ARB_FIELDS)
    rows = "".join(f"<tr><td>{esc(r['scan_time'])}</td><td>{esc(r['set'])}</td><td>{esc(r['sport'])}</td><td>{esc(r['event'])}</td>"
                   f"<td>{esc(r['commence'])}</td><td>{float(r['profit_pct']):.2f}%</td><td>{esc(r['legs'])}</td></tr>"
                   for _, r in arbs.iloc[::-1].head(200).iterrows())
    body = ("<h1>Sports arbitrage measurement</h1>"
            f"<p class='meta'>{s['scans']} scans over {s['days']:.1f} days, {s['events']} event snapshots. Capital assumed for the "
            f"estimates: {capital:,.0f} SEK. Nothing was bet.</p>"
            "<div class='cards'>" + "".join(
                f"<div class='card'><div class='l'>{esc(l)}</div><div class='v'>{v}</div></div>" for l, v in (
                    ("Arbitrages (SE books)", s["arbs_se"]), ("Avg profit (SE)", f"{s['avg_profit_se_pct']:.2f}%"),
                    ("Best profit (SE)", f"{s['max_profit_se_pct']:.2f}%"), ("SEK/week if all caught (SE)", f"{s['sek_per_week_se']:,.0f}"),
                    ("Arbitrages (all books)", s["arbs_all"]), ("SEK/week if all caught (all)", f"{s['sek_per_week_all']:,.0f}"),
                    ("Median margin (SE best odds)", f"{(s.get('median_margin_se_pct') or 0):.2f}%"),
                    ("Events within 1% of arb", f"{(s.get('share_near_arb_pct') or 0):.1f}%"))) + "</div>"
            "<h2>Arbitrages found</h2><div class='scroll'><table><tr><th>Scan</th><th>Set</th><th>Sport</th><th>Event</th>"
            "<th>Starts</th><th>Profit</th><th>Legs</th></tr>" + rows + "</table></div>"
            "<p class='small'>SE = Swedish-licensed bookmakers only (the ones a Swedish resident can legally use with tax-free winnings). "
            "'All' adds sharp books and the Betfair exchange as a reference; those are not licensed in Sweden. Profit is before "
            "any odds movement between spotting and placing both legs, and bookmakers limit winning accounts.</p>")
    out = ROOT / "reports" / "report_sportsarb.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page("Sports arbitrage measurement", body), encoding="utf-8")
    print(f"\nReport saved: {out}")
    if args.open:
        os.startfile(out)  # noqa: S606
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Sports arbitrage measurement.")
    ap.add_argument("command", choices=["scan", "run", "report"])
    ap.add_argument("--config", default="config_sportsarb.json")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg = load_config(ROOT / args.config)
    return {"scan": cmd_scan, "run": cmd_run, "report": cmd_report}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
