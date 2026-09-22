"""HTML reports for backtests and journals. Everything is inline, nothing is fetched from the web."""
from __future__ import annotations

import html
import math

import pandas as pd

from .config import fmt_ms

CSS = """
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;margin:0;background:#f4f5f7;color:#1c1e21}
.wrap{max-width:1120px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:18px;margin:34px 0 12px;border-bottom:1px solid #d8dbe0;padding-bottom:6px}
h3{font-size:15px;margin:22px 0 8px}
.meta{color:#5b6270;font-size:14px;margin-bottom:18px;line-height:1.5}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:#fff;border:1px solid #e1e4e8;border-radius:10px;padding:12px 14px}
.card .l{font-size:12px;color:#5b6270;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
.pos{color:#1a7f37}.neg{color:#c62828}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e1e4e8;font-size:13.5px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid #eef0f3;white-space:nowrap}
th{background:#f0f2f5;font-weight:600;color:#3c4453}
td:first-child,th:first-child{text-align:left}
.verdict{border-radius:10px;padding:14px 16px;margin:12px 0;line-height:1.55;background:#fff8e1;border:1px solid #f0d58c}
.verdict.ok{background:#e8f5e9;border-color:#a5d6a7}.verdict.bad{background:#ffebee;border-color:#ef9a9a}
.chart{background:#fff;border:1px solid #e1e4e8;border-radius:10px;padding:12px}
.small{font-size:13px;color:#5b6270;line-height:1.5}
dl dt{font-weight:600;margin-top:12px}dl dd{margin:3px 0 0 0;color:#3c4453;line-height:1.5}
"""
REASON_LABEL = {"stop": "stop-loss", "signal": "signal", "max_time": "max holding time",
                "kill_switch": "kill switch", "end_of_data": "end of data"}


# ---------------- small helpers ----------------

def _bad(x) -> bool:
    if x is None:
        return True
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True


def _num(x, dec: int = 2, suffix: str = "") -> str:
    if isinstance(x, float) and math.isinf(x):
        return "∞"
    if _bad(x):
        return "–"
    return f"{float(x):,.{dec}f}{suffix}"


def _pct(x, dec: int = 2) -> str:
    if _bad(x) or (isinstance(x, float) and math.isinf(x)):
        return "–"
    return f"{float(x):+.{dec}f}%"


def _cls(x) -> str:
    if _bad(x):
        return ""
    x = float(x)
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def esc(s) -> str:
    return html.escape(str(s))


def td(value, cls: str = "") -> str:
    return f'<td class="{cls}">{value}</td>' if cls else f"<td>{value}</td>"


def _capital(cfg: dict) -> tuple[float, str, float | None]:
    """(starting capital, currency, SEK rate or None if the account is already in SEK)."""
    cap = cfg["capital"]
    ccy = str(cap.get("currency", "USDT"))
    start = float(cap.get("start", 0.0))
    sek = None
    if ccy != "SEK":
        sek = float(cap.get("sek_per_unit", 0) or 0) or None
    return start, ccy, sek


# ---------------- verdict ----------------

def verdict(stats: dict, buyhold: dict | None = None) -> tuple[str, str]:
    """Returns (class, text). Class: ok / warn / bad."""
    n = int(stats.get("count", 0))
    bh_txt = ""
    if buyhold:
        parts = ", ".join(f"{s} {v:+.1f}%" for s, v in buyhold.items())
        bh_txt = f" Simply buying and holding would have returned: {parts}."
    if n < 30:
        return "warn", (f"Only {n} trades. That is too few to tell luck from skill; you want hundreds.{bh_txt}")
    ev = float(stats.get("expectancy_pct", 0.0))
    pf = float(stats.get("profit_factor", 0.0))
    ret = float(stats.get("return_pct", 0.0))
    dd = float(stats.get("max_dd_pct", 0.0))
    base = (f"{ev:+.2f}% per trade after fees, profit factor {_num(pf)}, total {ret:+.1f}% over {n} trades, "
            f"max drawdown {dd:+.1f}%.")
    if stats.get("kill_switch_date"):
        base += (f" The kill switch at -{float(stats.get('kill_switch_limit', 0)):.0f}% would have fired on "
                 f"{stats['kill_switch_date'][:10]} and shut the bot down.")
    if ev > 0 and pf >= 1.15:
        return "ok", ("Positive expectancy: " + base + bh_txt +
                      " Promising, but it may be luck or a fit to this particular period. "
                      "Must be confirmed in paper trading before real money.")
    if ev > 0:
        return "warn", ("Marginally positive: " + base + bh_txt +
                        " The edge is so thin that slightly worse fills or fees would eat it. Not enough for real money.")
    return "bad", ("No edge: " + base + bh_txt +
                   " The strategy loses money on this data and should not be run live as is.")


# ---------------- building blocks ----------------

def cards(stats: dict, buyhold: dict | None = None, sek: float | None = None, ccy: str = "USDT") -> str:
    items = [
        ("Return", _pct(stats.get("return_pct")), _cls(stats.get("return_pct"))),
        ("End equity", f"{_num(stats.get('end_equity'))} {ccy}", ""),
        ("Trades", str(int(stats.get("count", 0))), ""),
        ("Win rate", _num(stats.get("win_rate_pct"), 1, "%"), ""),
        ("Avg win", _pct(stats.get("avg_win_pct")), "pos"),
        ("Avg loss", _pct(stats.get("avg_loss_pct")), "neg"),
        ("Payoff", _num(stats.get("payoff")), ""),
        ("Profit factor", _num(stats.get("profit_factor")), ""),
        ("Expectancy / trade", _pct(stats.get("expectancy_pct")), _cls(stats.get("expectancy_pct"))),
        ("Max drawdown", _pct(stats.get("max_dd_pct")), "neg"),
        ("Fees total", f"{_num(stats.get('fees'))} {ccy}", ""),
        ("Return before fees", _pct(stats.get("return_before_fees_pct")), _cls(stats.get("return_before_fees_pct"))),
        ("Longest losing streak", str(int(stats.get("losing_streak", 0) or 0)), ""),
    ]
    if sek:
        items.insert(2, ("In SEK", f"~{_num(float(stats.get('end_equity', 0)) * sek, 0)} SEK", ""))
    for s, v in (buyhold or {}).items():
        items.append((f"Buy & hold {s}", _pct(v), _cls(v)))
    return '<div class="cards">' + "".join(
        f'<div class="card"><div class="l">{esc(l)}</div><div class="v {c}">{v}</div></div>'
        for l, v, c in items) + "</div>"


def svg_chart(series, xlabels=None, width: int = 1000, height: int = 340) -> str:
    """series: list of (name, values, color). All values are drawn against their index."""
    pad_l, pad_r, pad_t, pad_b = 60, 16, 16, 30
    vals = [float(v) for _, ys, _ in series for v in ys if not _bad(v)]
    if not vals:
        return "<p class='small'>No equity curve to show.</p>"
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        hi = lo + 1
    margin = (hi - lo) * 0.05
    lo -= margin
    hi += margin
    n = max(len(ys) for _, ys, _ in series)

    def x_of(i: int) -> float:
        return pad_l + (i / max(n - 1, 1)) * (width - pad_l - pad_r)

    def y_of(v: float) -> float:
        return pad_t + (hi - v) / (hi - lo) * (height - pad_t - pad_b)

    out = [f'<svg viewBox="0 0 {width} {height}" width="100%" style="display:block;font-family:inherit">']
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = y_of(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e5e8ec"/>')
        out.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" font-size="11" fill="#5b6270" text-anchor="end">{v:.1f}</text>')
    for frac, label in (xlabels or []):
        x = pad_l + frac * (width - pad_l - pad_r)
        out.append(f'<text x="{x:.1f}" y="{height - 8}" font-size="11" fill="#5b6270" text-anchor="middle">{esc(label)}</text>')
    for _label, ys, color in series:
        pts = " ".join(f"{x_of(i):.1f},{y_of(float(v)):.1f}" for i, v in enumerate(ys) if not _bad(v))
        out.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.6" points="{pts}"/>')
    lx = pad_l + 8
    for label, _ys, color in series:
        out.append(f'<rect x="{lx}" y="{pad_t + 2}" width="14" height="4" fill="{color}"/>')
        out.append(f'<text x="{lx + 18}" y="{pad_t + 7}" font-size="12" fill="#3c4453">{esc(label)}</text>')
        lx += 30 + 7 * len(label)
    out.append("</svg>")
    return "".join(out)


def equity_chart(equity: pd.DataFrame, start_cap: float, bh_cols: list[str]) -> str:
    if equity is None or len(equity) == 0:
        return "<p class='small'>No equity curve yet.</p>"
    eq = [float(v) / start_cap * 100 for v in equity["equity"]]
    series = [("Bot", eq, "#1f6feb")]
    colors = ["#e36209", "#8250df", "#1a7f37", "#c62828"]
    for i, c in enumerate(bh_cols[:4]):
        if c in equity.columns:
            vals = [float(v) / start_cap * 100 if not _bad(v) else float("nan") for v in equity[c]]
            series.append((f"Buy & hold {c[3:]}", vals, colors[i % len(colors)]))
    ms = [int(v) for v in equity["ms"]]
    n = len(ms)
    xlabels = None
    if n > 1:
        xlabels = [(f, fmt_ms(ms[int(round(f * (n - 1)))])[:10]) for f in (0, 0.25, 0.5, 0.75, 1.0)]
    return ('<div class="chart">' + svg_chart(series, xlabels) +
            '<p class="small">Start = 100. The curve shows equity as a percentage of starting capital, '
            'compared with simply buying and holding.</p></div>')


def strategy_table(results) -> str:
    head = ("<tr><th>Strategy</th><th>Return</th><th>Trades</th><th>Win rate</th><th>Avg win</th>"
            "<th>Avg loss</th><th>Payoff</th><th>Profit factor</th><th>Expectancy / trade</th>"
            "<th>Max drawdown</th><th>Stopped out</th><th>Verdict</th></tr>")
    rows = []
    for r in results:
        s = r.stats
        k, _ = verdict(s)
        badge = {"ok": "Promising", "warn": "Uncertain", "bad": "No edge"}[k]
        rows.append("<tr>" + td(esc(r.label))
                    + td(_pct(s.get("return_pct")), _cls(s.get("return_pct")))
                    + td(int(s.get("count", 0))) + td(_num(s.get("win_rate_pct"), 1, "%"))
                    + td(_pct(s.get("avg_win_pct")), "pos") + td(_pct(s.get("avg_loss_pct")), "neg")
                    + td(_num(s.get("payoff"))) + td(_num(s.get("profit_factor")))
                    + td(_pct(s.get("expectancy_pct")), _cls(s.get("expectancy_pct")))
                    + td(_pct(s.get("max_dd_pct")), "neg") + td(_num(s.get("stop_share_pct"), 0, "%"))
                    + td(badge, {"ok": "pos", "warn": "", "bad": "neg"}[k]) + "</tr>")
    return '<div class="scroll"><table>' + head + "".join(rows) + "</table></div>"


def group_table(rows: list[dict], first_col: str, ccy: str = "USDT", names: dict | None = None) -> str:
    if not rows:
        return "<p class='small'>No trades.</p>"
    names = names or {}
    head = (f"<tr><th>{esc(first_col)}</th><th>Trades</th><th>Win rate</th><th>Avg win</th>"
            f"<th>Avg loss</th><th>Expectancy / trade</th><th>Result</th></tr>")
    body = "".join(
        "<tr>" + td(esc(names.get(r["name"], r["name"]))) + td(r["count"]) + td(_num(r["win_rate_pct"], 1, "%"))
        + td(_pct(r["avg_win_pct"]), "pos") + td(_pct(r["avg_loss_pct"]), "neg")
        + td(_pct(r["expectancy_pct"]), _cls(r["expectancy_pct"]))
        + td(f"{r['pnl']:+.2f} {ccy}", _cls(r["pnl"])) + "</tr>"
        for r in rows)
    return '<div class="scroll"><table>' + head + body + "</table></div>"


def trades_table(trades: pd.DataFrame, limit: int = 80, names: dict | None = None) -> str:
    if trades is None or len(trades) == 0:
        return "<p class='small'>No closed trades.</p>"
    names = names or {}
    t = trades.tail(limit).iloc[::-1]
    head = ("<tr><th>#</th><th>Symbol</th><th>Strategy</th><th>Bought</th><th>Entry</th><th>Sold</th>"
            "<th>Exit</th><th>Cost</th><th>Result</th><th>%</th><th>Reason</th><th>Bars</th></tr>")
    rows = []
    for _, r in t.iterrows():
        pnl = float(r["pnl"])
        pct = float(r["pnl_pct"])
        rows.append("<tr>" + td(int(r["id"])) + td(esc(names.get(r["symbol"], r["symbol"]))) + td(esc(r["strategy"]))
                    + td(esc(r["entry_time"])) + td(_num(r["entry_price"])) + td(esc(r["exit_time"]))
                    + td(_num(r["exit_price"])) + td(_num(r["cost"]))
                    + td(f"{pnl:+.2f}", _cls(pnl)) + td(_pct(pct), _cls(pct))
                    + td(esc(REASON_LABEL.get(str(r["reason"]), r["reason"]))) + td(int(r["bars"])) + "</tr>")
    note = f"<p class='small'>Showing the latest {min(limit, len(trades))} of {len(trades)} trades, newest first.</p>"
    return note + '<div class="scroll"><table>' + head + "".join(rows) + "</table></div>"


def positions_table(positions: list[dict], prices: dict, ccy: str = "USDT", names: dict | None = None) -> str:
    if not positions:
        return "<p class='small'>No open positions right now.</p>"
    names = names or {}
    head = ("<tr><th>Symbol</th><th>Strategy</th><th>Bought</th><th>Entry</th><th>Last</th>"
            "<th>Stop</th><th>Cost</th><th>Unrealized</th><th>Bars</th></tr>")
    rows = []
    for p in positions:
        now = float(prices.get(p["symbol"], p["entry_price"]))
        unreal = (now - float(p["entry_price"])) * float(p["qty"])
        upct = (now / float(p["entry_price"]) - 1) * 100
        rows.append("<tr>" + td(esc(names.get(p["symbol"], p["symbol"]))) + td(esc(p["strategy"]))
                    + td(esc(fmt_ms(p["entry_ms"])))
                    + td(_num(p["entry_price"])) + td(_num(now)) + td(_num(p["stop"]))
                    + td(_num(p["cost"])) + td(f"{unreal:+.2f} {ccy} ({upct:+.2f}%)", _cls(unreal))
                    + td(int(p.get("bars", 0))) + "</tr>")
    return '<div class="scroll"><table>' + head + "".join(rows) + "</table></div>"


def pending_table(pending: list[dict], names: dict | None = None) -> str:
    if not pending:
        return ""
    names = names or {}
    rows = "".join(
        "<tr>" + td("Buy" if p["kind"] == "buy" else "Sell") + td(esc(names.get(p["symbol"], p["symbol"])))
        + td(esc(p["strategy"])) + td(_num(p.get("quote", 0), 0)) + td(_num(p.get("stop", 0)))
        + td(esc(p.get("reason", ""))) + td(esc(fmt_ms(p["signal_ms"])[:10])) + "</tr>"
        for p in pending)
    return ("<h2>Pending orders (filled at the next open)</h2><div class='scroll'><table>"
            "<tr><th>Order</th><th>Symbol</th><th>Strategy</th><th>Amount</th><th>Stop</th><th>Reason</th>"
            "<th>Signal</th></tr>" + rows + "</table></div>")


def glossary() -> str:
    items = [
        ("Return", "How much equity grew or shrank over the period, after fees and slippage."),
        ("Win rate", "Share of trades that made money. A high win rate is not the same as profitable: a strategy can win "
                     "80% of the time and still lose money if the losses are large."),
        ("Avg win / avg loss", "Average result in percent for winning and losing trades respectively."),
        ("Payoff", "Average win divided by average loss. Above 1 means wins are on average bigger than losses."),
        ("Profit factor", "Sum of all wins divided by the sum of all losses. Below 1 = losing money. "
                          "1.3 or more is usually considered good over the long run."),
        ("Expectancy per trade", "What an average trade returns in percent. The most important number: if it is negative "
                                 "you lose money no matter how often you are right."),
        ("Max drawdown", "The largest fall from a peak to a subsequent trough in the equity curve. Tells you how much it "
                         "will hurt along the way."),
        ("Stopped out", "Share of trades that were closed by the stop-loss rather than by a sell signal."),
        ("Longest losing streak", "Most losing trades in a row. Expect to live through it for real."),
        ("Buy & hold", "What you would have got by buying at the start of the period and doing nothing. If the bot does "
                       "not beat that there is no reason to trade."),
        ("Slippage", "The difference between the price you saw and the price you actually got. The backtest assumes a "
                     "small disadvantage like that on every trade."),
    ]
    return "<dl>" + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in items) + "</dl>"


def page(title: str, body: str) -> str:
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><div class='wrap'>{body}</div></body></html>")


# ---------------- full reports ----------------

def backtest_report(cfg: dict, results: list, combined, meta: dict) -> str:
    start_cap, ccy, sek = _capital(cfg)
    names = dict(cfg.get("symbol_names", {}))
    risk = cfg["risk"]
    fee_txt = f"Fee {risk['fee_pct']}%"
    if float(risk.get("min_fee", 0) or 0) > 0:
        fee_txt += f" (min {_num(risk['min_fee'], 0)} {ccy})"
    k, text = verdict(combined.stats, combined.buyhold)
    parts = [
        f"<h1>Backtest {esc(meta['timeframe'])}: {esc(', '.join(meta['symbols']))}</h1>",
        f"<div class='meta'>Period {esc(fmt_ms(combined.start_ms)[:10])} to {esc(fmt_ms(combined.end_ms)[:10])} "
        f"({meta['days']} days, about {meta['bars']} bars per symbol). Starting capital {_num(start_cap)} {ccy}. "
        f"{fee_txt} + slippage {risk['slippage_pct']}% on every buy and every sell. "
        f"Max {risk['max_open_positions']} position(s) at a time, daily halt at -{risk['max_daily_loss_pct']}%, "
        f"kill switch at -{risk['max_drawdown_pct']}% from the peak. Generated {esc(meta['created'])}.</div>",
        f"<div class='verdict {k}'><b>Verdict, all strategies together:</b> {esc(text)}</div>",
        "<h2>All strategies together (this is how paper trading runs)</h2>",
        cards(combined.stats, combined.buyhold, sek, ccy),
        "<h2>Equity curve</h2>",
        equity_chart(combined.equity, start_cap, [c for c in combined.equity.columns if str(c).startswith("bh_")]),
        "<h2>Each strategy on its own</h2>",
        "<p class='small'>Each row is a separate run with only that strategy enabled, same capital and risk rules.</p>",
        strategy_table(results),
    ]
    for r in results:
        k2, t2 = verdict(r.stats, None)
        parts.append(f"<h3>{esc(r.label)}</h3>")
        desc = meta.get("descriptions", {}).get(r.label, "")
        if desc:
            parts.append(f"<p class='small'>{esc(desc)}</p>")
        parts.append(f"<div class='verdict {k2}'>{esc(t2)}</div>")
        parts.append(group_table(r.stats.get("per_symbol", []), "Symbol", ccy, names))
    parts += [
        "<h2>Per strategy in the combined run</h2>",
        group_table(combined.stats.get("per_strategy", []), "Strategy", ccy),
        "<h2>Per symbol in the combined run</h2>",
        group_table(combined.stats.get("per_symbol", []), "Symbol", ccy, names),
        "<h2>Trades in the combined run</h2>",
        trades_table(combined.trades, 80, names),
        "<h2>Glossary</h2>",
        glossary(),
    ]
    return page(f"Backtest {meta.get('heading', meta['timeframe'])}", "".join(parts))


def journal_report(cfg: dict, mode: str, stats: dict, trades: pd.DataFrame, equity: pd.DataFrame,
                   positions: list[dict], meta: dict) -> str:
    _cfg_start, ccy, sek = _capital(cfg)
    start_cap = float(stats.get("start_equity", _cfg_start))
    names = dict(cfg.get("symbol_names", {}))
    k, text = verdict(stats, None)
    period = ""
    if len(equity):
        period = f"From {esc(str(equity['time'].iloc[0]))} to {esc(str(equity['time'].iloc[-1]))}. "
    risk_state = meta.get("risk", {})
    warning = ""
    if risk_state.get("killed"):
        warning = f"<div class='verdict bad'><b>Kill switch is active.</b> {esc(risk_state.get('kill_reason', ''))}</div>"
    elif risk_state.get("halted_today"):
        warning = "<div class='verdict'><b>Daily halt active:</b> no new buys until tomorrow.</div>"
    heading = meta.get("heading") or ("paper trading" if mode == "paper" else "live trading")
    symbols_txt = ", ".join(names.get(s, s) for s in cfg["symbols"])
    parts = [
        f"<h1>Journal: {esc(heading)}</h1>",
        f"<div class='meta'>{period}{esc(symbols_txt)} on {esc(cfg['timeframe'])} bars. "
        f"Starting capital {_num(start_cap)} {ccy}. Generated {esc(meta.get('created', ''))}.</div>",
        warning,
        f"<div class='verdict {k}'>{esc(text)}</div>",
        cards(stats, None, sek, ccy),
        "<h2>Equity curve</h2>", equity_chart(equity, start_cap, []),
        "<h2>Open positions</h2>", positions_table(positions, meta.get("prices", {}), ccy, names),
        pending_table(meta.get("pending", []), names),
        "<h2>Per strategy</h2>", group_table(stats.get("per_strategy", []), "Strategy", ccy),
        "<h2>Per symbol</h2>", group_table(stats.get("per_symbol", []), "Symbol", ccy, names),
        "<h2>Trades</h2>", trades_table(trades, 200, names),
        "<h2>Glossary</h2>", glossary(),
    ]
    return page(f"Journal {heading}", "".join(parts))
