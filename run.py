"""Runs a bot. The mode comes from "mode" in the config: "paper" (simulated money) or "live" (real Binance orders).

    py run.py                                 crypto bot (config.json), runs until you close the window
    py run.py --config config_stocks_se.json  Swedish stocks bot (signals + paper trading)
    py run.py --once                          run a single cycle and exit (handy for testing)
    py run.py --mode paper                    override "mode" in the config
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot import data_stocks
from bot.backtest import make_paper_broker
from bot.broker import BinanceBroker
from bot.config import ROOT, fmt_num, load_config, load_secrets, ms_to_local, now_ms
from bot.data import fetch_recent as fetch_recent_crypto
from bot.engine import Intent, Trader
from bot.journal import Journal, compute_stats
from bot.notify import Notifier
from bot.risk import RiskManager
from bot.strategies import load_strategies

BOT_LABELS = {"stocks_se": "Swedish stocks bot", "stocks_us": "US stocks bot", "crypto": "Crypto bot"}
START_BAT = {"stocks_se": "stocks_se_start.bat", "stocks_us": "stocks_us_start.bat", "crypto": "start.bat"}
DECISION_TIMES = {"4h": "at 02:00, 06:00, 10:00, 14:00, 18:00 and 22:00 Stockholm time", "1h": "every full hour",
                  "2h": "every second hour", "1d": "at 02:00 Stockholm time", "15m": "every 15 minutes",
                  "30m": "every half hour", "5m": "every 5 minutes"}


def setup_logging(path: Path):
    logger = logging.getLogger("bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger.info


def load_state(path: Path) -> dict:
    if path.exists() and path.stat().st_size > 0:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {"started_ms": now_ms()}
    state.setdefault("last_candle", {})
    state.setdefault("report_day", "")
    state.setdefault("pending", [])
    return state


def save_state(path: Path, state: dict, trader: Trader, risk: RiskManager) -> None:
    state["trader"] = trader.to_dict()
    state["risk"] = risk.to_dict()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


class Bot:
    def __init__(self, cfg, mode, log, broker, strategies, risk, trader, journal, notifier, state, state_path):
        self.cfg = cfg
        self.mode = mode
        self.market = cfg.get("market", "crypto")
        self.is_stocks = self.market in data_stocks.MARKETS
        self.mkt = data_stocks.get_market(self.market) if self.is_stocks else None
        self.next_open = cfg.get("execution", "immediate") == "next_open"
        self.log = log
        self.broker = broker
        self.strategies = strategies
        self.strat_by_name = {s.name: s for s in strategies}
        self.risk = risk
        self.trader = trader
        self.journal = journal
        self.notifier = notifier
        self.state = state
        self.state_path = state_path
        self._sync_reserved()

    @property
    def bot_name(self) -> str:
        return BOT_LABELS.get(self.market, "Crypto bot")

    # ---------- order execution ----------
    def execute_buy(self, intent: Intent, strat, ref_price: float) -> None:
        t = self.trader
        if self.mode == "live":
            free = self.broker.balance("USDT")
            if free < intent.quote:
                self.log(f"Skipping buy: {free:.2f} USDT free on Binance, need {intent.quote:.2f}")
                return
        quote = min(intent.quote, t.cash * 0.999)
        if quote < self.risk.min_notional:
            self.log(f"Skipping buy of {t.name(intent.key[0])}: only {t.money(t.cash)} in cash")
            return
        intent.quote = round(quote, 2)
        fill = self.broker.buy(intent.key[0], intent.quote, ref_price, now_ms())
        if fill is None:
            self.log(f"Skipping buy of {t.name(intent.key[0])}: the amount does not cover one whole share")
            return
        t.apply_buy(intent, fill, strat)

    def execute_sell(self, key, ref_price: float, reason: str) -> None:
        pos = self.trader.positions.get(key)
        if pos is None:
            return
        fill = self.broker.sell(pos.symbol, pos.qty, ref_price, now_ms())
        self.trader.apply_sell(key, fill, reason)

    # ---------- pending orders (stocks: filled at the next open) ----------
    def _sync_reserved(self) -> None:
        self.trader.reserved_symbols = {p["symbol"] for p in self.state["pending"] if p["kind"] == "buy"}

    def queue(self, intent: Intent, bar_close: float, signal_ms: int) -> None:
        t = self.trader
        symbol, sname = intent.key
        self.state["pending"].append({
            "kind": intent.kind, "symbol": symbol, "strategy": sname, "quote": intent.quote,
            "stop": intent.stop, "atr": intent.atr, "reason": intent.reason, "signal_ms": int(signal_ms),
        })
        self._sync_reserved()
        name = t.name(symbol)
        money_kind = "simulated money" if t.paper else "real money"
        open_label = self.mkt.open_label if self.mkt else "the next open"
        if intent.kind == "buy":
            est = int(intent.quote // bar_close) if bar_close > 0 else 0
            risk_amt = max(0.0, (bar_close - intent.stop) * est)
            msg = (f"Why: {t.why_buy(sname)}.\n"
                   f"What the bot does: buys at the next open ({open_label}) for about {t.money(intent.quote)}, "
                   f"roughly {est} shares at {fmt_num(bar_close)}, with {money_kind}.\n"
                   f"To do the same on Avanza: place a buy order for {est} shares before the open and a stop-loss at "
                   f"{fmt_num(intent.stop)} ({(intent.stop / bar_close - 1) * 100:+.1f}%). A stop-loss sells automatically "
                   f"if price falls there, capping the loss at about {t.money(risk_amt)}.")
            title = f"Buy signal: {name} [{t.strat_label(sname)}]"
        else:
            pos = t.positions.get(intent.key)
            msg = (f"Why: {t.why_sell(sname, intent.reason, pos)}.\n"
                   f"What the bot does: sells at the next open ({open_label}).\n"
                   f"If you hold it on Avanza: place a sell order before the open.")
            title = f"Sell signal: {name}"
        self.log(f"{title}: " + msg.replace("\n", " "))
        self.notifier.send(title + t.label(), msg, tags=["bell"])
        self.notifier.send_public(title, f"{t.bot_label}.\n{msg}", tags=["bell"])

    def run_pending(self, symbol: str, bar_open_ms: int, open_price: float, fresh: bool) -> None:
        """Fills pending orders for the symbol once a newer bar with an open price exists.
        fresh=True means the bar is today's live bar, i.e. the open price is from today.
        If the bot was down during the day, the order waits for the next open instead of being booked in hindsight."""
        kept = []
        for p in self.state["pending"]:
            if not fresh or p["symbol"] != symbol or bar_open_ms <= int(p["signal_ms"]):
                kept.append(p)
                continue
            key = (symbol, p["strategy"])
            strat = self.strat_by_name.get(p["strategy"])
            try:
                if p["kind"] == "sell":
                    self.execute_sell(key, open_price, p["reason"])
                else:
                    self.trader.reserved_symbols.discard(symbol)
                    ok, why = self.risk.can_open(self.trader.n_open())
                    if strat is None or not ok or self.trader.has_position_in(symbol):
                        self.log(f"Pending buy of {self.trader.name(symbol)} skipped: {why or 'position already exists'}")
                    else:
                        it = Intent("buy", key, quote=float(p["quote"]), stop=float(p["stop"]), atr=float(p["atr"]))
                        self.execute_buy(it, strat, open_price)
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not fill pending order for {self.trader.name(symbol)}: {e}")
        self.state["pending"] = kept
        self._sync_reserved()

    # ---------- one cycle ----------
    def cycle(self) -> None:
        if self.is_stocks:
            self._cycle_stocks()
        else:
            self._cycle_crypto()

    def _handle_bar(self, symbol: str, strat, closed, price_now: float) -> bool:
        """Evaluates the latest closed bar for (symbol, strategy). Returns True if it was new."""
        key = (symbol, strat.name)
        skey = f"{symbol}|{strat.name}"
        last_open = int(closed["open_time"].iloc[-1])
        if self.state["last_candle"].get(skey) == last_open:
            return False
        row = strat.prepare(closed).iloc[-1]
        bar = {"close": float(row["close"]), "entry": bool(row["entry"]),
               "exit": bool(row["exit"]), "atr": float(row["atr"])}
        for it in self.trader.evaluate_bar(key, bar, strat):
            if self.next_open:
                self.queue(it, bar["close"], last_open)
            elif it.kind == "sell":
                self.execute_sell(key, price_now, it.reason)
            else:
                self.execute_buy(it, strat, price_now)
        self.state["last_candle"][skey] = last_open
        return True

    def _finish_cycle(self, now: int, prices: dict, new_candle: bool) -> None:
        self.trader.mark(now, prices, record=new_candle)
        if self.risk.killed and self.trader.positions:
            self.trader.close_all(now, prices, "kill_switch")
        if new_candle:
            self.status(prices)
        self.daily_report(now)
        save_state(self.state_path, self.state, self.trader, self.risk)

    def _cycle_crypto(self) -> None:
        cfg = self.cfg
        now = now_ms()
        self.risk.new_time(now, self.trader.equity())
        prices: dict[str, float] = {}
        new_candle = False
        for symbol in cfg["symbols"]:
            closed, current = fetch_recent_crypto(symbol, cfg["timeframe"], int(cfg.get("lookback_bars", 1000)))
            if closed.empty:
                continue
            price_now = float(current["close"]) if current is not None else float(closed["close"].iloc[-1])
            prices[symbol] = price_now
            for it in self.trader.stop_intents(symbol, price_now):
                self.execute_sell(it.key, price_now, it.reason)
            for strat in self.strategies:
                if self._handle_bar(symbol, strat, closed, price_now):
                    new_candle = True
        self._finish_cycle(now, prices, new_candle)

    def _cycle_stocks(self) -> None:
        cfg = self.cfg
        now = now_ms()
        self.risk.new_time(now, self.trader.equity())
        recent = data_stocks.fetch_recent(cfg["symbols"], period="2y", market=self.mkt)
        prices: dict[str, float] = {}
        new_candle = False
        for symbol in cfg["symbols"]:
            closed, current = recent.get(symbol, (None, None))
            if closed is None or closed.empty:
                continue
            latest = current if current is not None else closed.iloc[-1]
            price_now = float(latest["close"])
            prices[symbol] = price_now
            self.run_pending(symbol, int(latest["open_time"]), float(latest["open"]), fresh=current is not None)
            for it in self.trader.stop_intents(symbol, price_now):
                self.execute_sell(it.key, price_now, it.reason)
            for strat in self.strategies:
                if self._handle_bar(symbol, strat, closed, price_now):
                    new_candle = True
        self._finish_cycle(now, prices, new_candle)

    # ---------- status and reports ----------
    def _positions_text(self) -> str:
        t = self.trader
        parts = []
        for p in t.positions.values():
            last = t.last_prices.get(p.symbol, p.entry_price)
            parts.append(f"{t.name(p.symbol)} {(last / p.entry_price - 1) * 100:+.1f}% (stop {fmt_num(p.stop)})")
        return ", ".join(parts) or "none"

    def _pending_text(self) -> str:
        t = self.trader
        return ", ".join(f"{t.name(p['symbol'])} ({p['kind']} at the open)" for p in self.state["pending"])

    def status(self, prices: dict) -> None:
        t = self.trader
        eq = t.equity()
        pend = f" | pending orders: {self._pending_text()}" if self.state["pending"] else ""
        if self.is_stocks:
            quotes = f"{len(prices)} stocks updated"
        else:
            quotes = " ".join(f"{t.name(s)} {fmt_num(p)}" for s, p in prices.items())
        mode = " | cautious mode" if self.risk.cautious else ""
        self.log(f"Equity {t.money(eq)} ({(eq / t.start_equity - 1) * 100:+.2f}% since start) | "
                 f"cash {t.money(t.cash)} | positions: {self._positions_text()}{pend}{mode} | {quotes}")

    def daily_report(self, now: int) -> None:
        hour = int(self.cfg["ntfy"].get("daily_report_hour", 20))
        local = ms_to_local(now)
        day = local.strftime("%Y-%m-%d")
        if local.hour < hour or self.state.get("report_day") == day:
            return
        self.state["report_day"] = day
        t = self.trader
        trades = self.journal.load_trades()
        stats = compute_stats(trades, self.journal.load_equity(), t.start_equity)
        today = trades[trades["exit_time"].astype(str).str.startswith(day)] if len(trades) else trades
        eq = t.equity()
        lines = [f"Equity: {t.money(eq)} ({(eq / t.start_equity - 1) * 100:+.1f}% since start)."]
        if len(today):
            lines.append(f"Today: {len(today)} closed trades, result {t.money(float(today['pnl'].sum()))}.")
        else:
            lines.append("Today: no closed trades.")
        n = int(stats["count"])
        if n >= 5:
            lines.append(f"Since start: {n} trades, {stats['win_rate_pct']:.0f}% winners, "
                         f"{stats['expectancy_pct']:+.2f}% per trade on average.")
        else:
            lines.append(f"Since start: {n} closed trades.")
        lines.append(f"Max drawdown from peak so far: {min(stats['max_dd_pct'], self.risk.max_dd):.1f}%.")
        lines.append(f"Open positions: {self._positions_text()}.")
        if self.state["pending"]:
            lines.append(f"Pending orders: {self._pending_text()}.")
        if self.risk.cautious:
            lines.append("Cautious mode: the bot buys at half size until the drawdown shrinks.")
        msg = "\n".join(lines)
        self.log("Daily report: " + msg.replace("\n", " "))
        self.notifier.send(f"Daily report: {self.bot_name}{t.label()}", msg, tags=["bar_chart"])


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a trading bot (paper or live).")
    ap.add_argument("--config", default="config.json", help="configuration file, e.g. config_stocks_se.json")
    ap.add_argument("--mode", choices=["paper", "live"], help="override 'mode' in the configuration")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = ap.parse_args()

    cfg = load_config(ROOT / args.config)
    secrets = load_secrets()
    market = cfg.get("market", "crypto")
    mode = args.mode or cfg.get("mode", "paper")
    if mode not in ("paper", "live"):
        print(f"Unknown mode '{mode}'. Use 'paper' or 'live'.")
        return 1
    is_stocks = market in data_stocks.MARKETS
    mkt = data_stocks.get_market(market) if is_stocks else None
    if is_stocks and mode == "live":
        print("Stocks have no broker API here (Avanza). Run in 'paper' mode: the bot sends signals and you place orders.")
        return 1
    journal_name = cfg.get("journal", mode)
    jdir = ROOT / "journal" / journal_name
    jdir.mkdir(parents=True, exist_ok=True)
    log = setup_logging(jdir / "bot.log")
    nt = cfg.get("ntfy", {})
    notifier = Notifier(nt.get("server", "https://ntfy.sh"), secrets.get("ntfy_topic", ""), log,
                        public_topic=nt.get("public_topic", ""), public_url=nt.get("public_url", ""))
    fee = float(cfg["risk"].get("fee_pct", 0.1))
    bot_name = BOT_LABELS.get(market, "Crypto bot")
    paper = mode == "paper"

    if mode == "live":
        testnet = bool(cfg.get("exchange", {}).get("testnet", False))
        try:
            broker = BinanceBroker(secrets.get("binance_api_key", ""), secrets.get("binance_api_secret", ""),
                                   testnet=testnet, fee_pct=fee)
            bal = broker.balances()
        except Exception as e:  # noqa: BLE001
            log(f"Could not connect to Binance: {e}")
            return 1
        balance_txt = ", ".join(f"{a} {v:.4f}" for a, v in sorted(bal.items())) or "empty"
        log(f"Connected to Binance{' TESTNET' if testnet else ''}. Free balance: {balance_txt}")
        if testnet:
            log("Testnet: play money in Binance's test environment, no real funds.")
        else:
            log("WARNING: LIVE MODE. The bot places real orders with real money.")
    else:
        broker = make_paper_broker(cfg)
        if is_stocks:
            log(f"Stocks, paper trading: signals after the close ({mkt.settle_label}), buys and sells are booked at "
                f"the next open ({mkt.open_label}). Place the orders yourself on Avanza if you want to follow them.")
        else:
            log("Paper trading: real prices, simulated money. No orders are sent to Binance.")

    strategies = load_strategies(cfg)
    start_cap = float(cfg["capital"]["start"])
    risk = RiskManager(cfg["risk"], start_cap)
    journal = Journal(jdir)
    trader = Trader(cfg, risk, broker, journal, notifier, log)
    trader.strategies = {s.name: s for s in strategies}
    trader.paper = paper
    trader.bot_label = {"stocks_se": "Swedish stocks bot (daily bars, signals only, paper)",
                        "stocks_us": "US stocks bot (daily bars, signals only, paper)"}.get(
        market, f"Crypto bot (Binance, {cfg['timeframe']} bars, {'paper' if paper else 'live'})")
    state_path = jdir / "state.json"
    state = load_state(state_path)
    if "trader" in state:
        trader.restore(state["trader"])
        risk.restore(state.get("risk", {}))
        log(f"Resuming: equity {trader.money(trader.equity())}, {len(trader.positions)} open positions, "
            f"{len(state['pending'])} pending orders, {trader.trade_counter} trades so far.")
    else:
        log(f"New account with {trader.money(start_cap)}.")
    if risk.killed:
        log(f"Kill switch is active: {risk.kill_reason} Delete journal/{journal_name}/state.json to start over.")
        return 1
    symbols_txt = ", ".join(trader.name(s) for s in cfg["symbols"])
    poll = int(cfg.get("poll_seconds", 30))
    log(f"Strategies: {', '.join(s.title for s in strategies)}. Symbols: {symbols_txt}. "
        f"Timeframe {cfg['timeframe']}. Push: {'on (' + notifier.topic + ')' if notifier.enabled else 'off'}"
        f"{', public topic ' + notifier.public_topic if notifier.public_enabled else ''}.")
    money_kind = "simulated money" if paper else "real money"
    if is_stocks:
        start_msg = (f"Watching {len(cfg['symbols'])} stocks: {symbols_txt}.\n"
                     f"Checks prices every {max(1, poll // 60)} minutes. Signals arrive after the close "
                     f"({mkt.settle_label}) and trades are booked at the next day's open ({mkt.open_label}).\n"
                     f"Equity: {trader.money(trader.equity())} {money_kind}.")
    else:
        start_msg = (f"Watching {symbols_txt} on {cfg['timeframe']} bars. Decisions are made when a bar closes, "
                     f"{DECISION_TIMES.get(cfg['timeframe'], 'per the timeframe')}.\n"
                     f"Equity: {trader.money(trader.equity())} {money_kind}.")
    notifier.send(f"{bot_name} is running ({'paper trading' if paper else 'LIVE'})", start_msg, tags=["robot"])

    bot = Bot(cfg, mode, log, broker, strategies, risk, trader, journal, notifier, state, state_path)
    errors_in_a_row = 0
    try:
        while True:
            try:
                bot.cycle()
                errors_in_a_row = 0
            except Exception as e:  # noqa: BLE001
                errors_in_a_row += 1
                log(f"Error in cycle ({errors_in_a_row} in a row): {e}")
                log(traceback.format_exc().strip().splitlines()[-1])
                if errors_in_a_row == 5:
                    notifier.send(f"{bot_name} has a problem", f"Five errors in a row. Latest: {e}",
                                  priority=4, tags=["warning"])
            if args.once:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        log("Stopped by the user. Positions and equity are saved in state.json.")
        save_state(state_path, state, trader, risk)
        notifier.send(f"{bot_name} stopped",
                      f"Equity {trader.money(trader.equity())}, {len(trader.positions)} open positions. "
                      f"Start it again with {START_BAT.get(market, 'start.bat')} and it continues where it left off.",
                      tags=["zzz"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
