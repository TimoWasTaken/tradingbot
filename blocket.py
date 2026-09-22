"""Blocket deal finder: scan, run, report, locations, flip.

    py blocket.py scan                one pass over all watches, prints new listings and deals (no push)
    py blocket.py run                 keeps watching (every poll_minutes), pushes every deal to your phone
    py blocket.py report              what has been seen, reference prices, deals, and your recorded flips
    py blocket.py locations           list the location codes Blocket accepts for the "location" setting
    py blocket.py flip --watch "PlayStation 5" --heading "PS5 disc" --bought 2500 --sold 3400 --costs 100
                                      record a flip you actually did, for the profit report
Nothing is bought or messaged automatically.
"""
from __future__ import annotations

import argparse
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

from bot import blocket as bl
from bot.config import ROOT, fmt_num, load_config, load_secrets
from bot.notify import Notifier
from bot.report import page


def esc(s) -> str:
    return html.escape(str(s))


def setup_logging(path: Path):
    logger = logging.getLogger("blocket")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger.info


def make(cfg: dict, log, quiet: bool = False):
    """Returns (DealFinder, TraderaWatcher or None, journal dir). Tradera is optional: no key, no Tradera."""
    secrets = load_secrets()
    jdir = ROOT / "journal" / cfg.get("journal", "blocket")
    notifier = Notifier(cfg.get("ntfy", {}).get("server", "https://ntfy.sh"), "" if quiet else secrets.get("ntfy_topic", ""), log)
    finder = bl.DealFinder(cfg, jdir, notifier, log)
    watcher = None
    if cfg.get("tradera", {}).get("enabled", True):
        try:
            from bot import tradera as tr
            api = tr.TraderaAPI(secrets.get("tradera_app_id", ""), secrets.get("tradera_app_key", ""))
            watcher = tr.TraderaWatcher(cfg, api, jdir, notifier, log, blocket_reference=finder.asking_reference)
            finder.sold_register = watcher.sold
        except ValueError as e:
            log(f"Tradera disabled: {e}")
    return finder, watcher, jdir


def cmd_scan(cfg: dict, args) -> int:
    finder, watcher, _ = make(cfg, print, quiet=True)
    if watcher:
        watcher.cycle()
    finder.cycle()
    return 0


def cmd_run(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "blocket")
    jdir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(jdir / "bot.log")
    finder, watcher, _ = make(cfg, log)
    poll = int(cfg.get("poll_minutes", 10))
    log(f"Blocket deal finder: {len(cfg['watches'])} watches ({', '.join(w['name'] for w in cfg['watches'])}), "
        f"checking every {poll} minutes, alert at {cfg.get('discount_alert_pct', 30)}% under the going price. "
        f"Tradera: {'on (real sale prices + ending auctions)' if watcher else 'off'}. Push: {'on' if finder.notifier.enabled else 'off'}.")

    def one_round() -> None:
        if watcher:
            try:
                watcher.cycle()
            except Exception as e:  # noqa: BLE001
                log(f"Tradera cycle failed: {e}")
        finder.cycle()

    if args.once:
        one_round()
        return 0
    try:
        while True:
            try:
                one_round()
            except Exception as e:  # noqa: BLE001
                log(f"Cycle failed: {e}")
                log(traceback.format_exc().strip().splitlines()[-1])
            time.sleep(poll * 60)
    except KeyboardInterrupt:
        finder.save()
        log("Stopped by the user.")
    return 0


def cmd_locations(cfg: dict, args) -> int:
    for value, name in bl.available_locations():
        print(f"  {value:24} {name}")
    print("\nPut the value in config_blocket.json as \"location\" (global) or per watch.")
    return 0


def cmd_flip(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "blocket")
    row = bl.add_flip(jdir, args.watch or "", args.heading or "", float(args.bought), float(args.sold), float(args.costs or 0), args.note or "")
    print(f"Recorded flip #{row['id']}: bought {row['bought']:.0f}, sold {row['sold']:.0f}, costs {row['costs']:.0f} -> profit {row['profit']:.0f} kr")
    return 0


def cmd_report(cfg: dict, args) -> int:
    jdir = ROOT / "journal" / cfg.get("journal", "blocket")
    listings = bl.load_csv(jdir, "listings.csv", bl.LISTING_FIELDS)
    alerts = bl.load_csv(jdir, "alerts.csv", bl.ALERT_FIELDS)
    flips = bl.load_csv(jdir, "flips.csv", bl.FLIP_FIELDS)
    finder, watcher, _ = make(cfg, print, quiet=True)
    sold = bl.load_csv(jdir, "sold.csv", ["recorded", "watch", "item_id", "title", "item_type", "final_price", "bids", "end_date", "url"])
    auctions = bl.load_csv(jdir, "auction_alerts.csv", ["id", "time", "ms", "watch", "item_id", "title", "current_bid", "next_bid",
                                                        "reference", "reference_kind", "discount_pct", "bids", "ends", "minutes_left", "url"])
    print(f"Listings seen: {len(listings)}, deals alerted: {len(alerts)}, Tradera sales recorded: {len(sold)}, "
          f"auction alerts: {len(auctions)}, flips recorded: {len(flips)}")
    rows = ""
    for w in cfg["watches"]:
        ref, n = finder.reference(w)
        kind = finder.last_reference_kind
        sub = listings[listings["watch"] == w["name"]] if len(listings) else listings
        ok = sub[sub["passed_filters"] == 1] if len(sub) else sub
        a = alerts[alerts["watch"] == w["name"]] if len(alerts) else alerts
        s = sold[sold["watch"] == w["name"]] if len(sold) else sold
        sp = pd.to_numeric(s["final_price"], errors="coerce") if len(s) else pd.Series(dtype=float)
        sp = sp[sp > 0]
        p = pd.to_numeric(ok["price"], errors="coerce") if len(ok) else pd.Series(dtype=float)
        print(f"  {w['name']:20} Blocket asks {len(ok):4} med {(f'{p.median():,.0f}' if len(p) else '-'):>7} | Tradera sold {len(sp):4} "
              f"med {(f'{sp.median():,.0f}' if len(sp) else '-'):>7} | ref {(f'{ref:,.0f} kr' if ref else 'learning'):>10} ({kind}) | deals {len(a)}")
        rows += (f"<tr><td>{esc(w['name'])}</td><td>{len(ok)}</td><td>{fmt_num(p.median(), 0) if len(p) else '–'}</td>"
                 f"<td>{len(sp)}</td><td>{fmt_num(sp.median(), 0) if len(sp) else '–'}</td>"
                 f"<td>{fmt_num(ref, 0) + ' kr' if ref else 'learning'}</td><td>{esc(kind)}</td><td>{len(a)}</td></tr>")
    arows = "".join(f"<tr><td>{int(r['id'])}</td><td>{esc(r['time'])}</td><td>{esc(r['watch'])}</td><td><a href='{esc(r['url'])}'>{esc(r['heading'])}</a></td>"
                    f"<td>{fmt_num(r['price'], 0)}</td><td>{fmt_num(r['reference'], 0)}</td><td>{float(r['discount_pct']):.0f}%</td>"
                    f"<td>{fmt_num(r['expected_margin'], 0)}</td><td>{esc(r['location'])}</td><td>{'yes' if int(r['shipping']) else ''}</td></tr>"
                    for _, r in alerts.iloc[::-1].head(200).iterrows())
    frows = "".join(f"<tr><td>{int(r['id'])}</td><td>{esc(r['time'])}</td><td>{esc(r['watch'])}</td><td>{esc(r['heading'])}</td>"
                    f"<td>{fmt_num(r['bought'], 0)}</td><td>{fmt_num(r['sold'], 0)}</td><td>{fmt_num(r['costs'], 0)}</td>"
                    f"<td class='{'pos' if float(r['profit']) > 0 else 'neg'}'>{fmt_num(r['profit'], 0)}</td></tr>" for _, r in flips.iterrows())
    total_profit = float(flips["profit"].sum()) if len(flips) else 0.0
    body = ("<h1>Blocket deal finder</h1>"
            f"<p class='meta'>{len(listings)} listings seen, {len(alerts)} deals alerted, {len(flips)} flips recorded "
            f"(total profit {fmt_num(total_profit, 0)} kr).</p>"
            "<h2>Watches</h2><div class='scroll'><table><tr><th>Watch</th><th>Blocket asks</th><th>Ask median</th><th>Tradera sales</th>"
            "<th>Sold median</th><th>Reference used</th><th>Kind</th><th>Deals</th></tr>" + rows + "</table></div>"
            "<h2>Auction alerts (Tradera, ending soon and cheap)</h2><div class='scroll'><table><tr><th>#</th><th>Time</th><th>Watch</th>"
            "<th>Listing</th><th>Next bid</th><th>Reference</th><th>Under</th><th>Bids</th><th>Ends</th></tr>"
            + "".join(f"<tr><td>{int(r['id'])}</td><td>{esc(r['time'])}</td><td>{esc(r['watch'])}</td><td><a href='{esc(r['url'])}'>{esc(r['title'])}</a></td>"
                      f"<td>{fmt_num(r['next_bid'], 0)}</td><td>{fmt_num(r['reference'], 0)}</td><td>{float(r['discount_pct']):.0f}%</td>"
                      f"<td>{int(r['bids'])}</td><td>{esc(r['ends'])}</td></tr>" for _, r in auctions.iloc[::-1].head(200).iterrows())
            + "</table></div>"
            "<h2>Deals alerted</h2><div class='scroll'><table><tr><th>#</th><th>Time</th><th>Watch</th><th>Listing</th><th>Price</th>"
            "<th>Reference</th><th>Under</th><th>Est. margin</th><th>Location</th><th>Ships</th></tr>" + arows + "</table></div>"
            "<h2>Flips you recorded</h2>" + ("<div class='scroll'><table><tr><th>#</th><th>Time</th><th>Watch</th><th>Item</th><th>Bought</th>"
            "<th>Sold</th><th>Costs</th><th>Profit</th></tr>" + frows + "</table></div>" if frows else
            "<p class='small'>None yet. Record one with: py blocket.py flip --watch ... --bought ... --sold ...</p>"))
    out = ROOT / "reports" / "report_blocket.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page("Blocket deal finder", body), encoding="utf-8")
    print(f"Report saved: {out}")
    if args.open:
        os.startfile(out)  # noqa: S606
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Blocket deal finder.")
    ap.add_argument("command", choices=["scan", "run", "report", "locations", "flip"])
    ap.add_argument("--config", default="config_blocket.json")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch")
    ap.add_argument("--heading")
    ap.add_argument("--bought", type=float)
    ap.add_argument("--sold", type=float)
    ap.add_argument("--costs", type=float, default=0.0)
    ap.add_argument("--note")
    args = ap.parse_args()
    cfg = load_config(ROOT / args.config)
    return {"scan": cmd_scan, "run": cmd_run, "report": cmd_report, "locations": cmd_locations, "flip": cmd_flip}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
