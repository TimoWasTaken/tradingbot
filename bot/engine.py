"""The core of every bot: cash, positions, stops and bookkeeping of trades.
Exactly the same code runs in backtests, paper trading and live trading, so what was tested on
history is also what trades for real. All log and notification texts are plain English with a "why"."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import fmt_ms, fmt_num, period_word

EVENT_TITLES = {"daily_halt": "Daily halt", "kill_switch": "KILL SWITCH",
                "cautious": "Cautious mode", "normal": "Normal mode again"}
EVENT_PRIO = {"daily_halt": 4, "kill_switch": 5}
REASON_TEXT = {"stop": "stop-loss hit: price fell to the level where the bot sells to cap the loss",
               "max_time": "maximum holding time reached without the trade taking off",
               "kill_switch": "kill switch triggered, the bot closes everything",
               "end_of_data": "end of data"}


@dataclass
class Position:
    symbol: str
    strategy: str
    qty: float
    entry_price: float
    entry_ms: int
    cost: float
    entry_fee: float
    stop: float
    trail_atr: float
    highest_close: float
    max_hold_bars: int
    bars: int = 0
    trade_id: int = 0


@dataclass
class Intent:
    """A desired order that the loop (backtest or live) executes."""
    kind: str            # "buy" or "sell"
    key: tuple           # (symbol, strategy name)
    quote: float = 0.0   # amount to buy for
    stop: float = 0.0
    reason: str = ""
    atr: float = 0.0


class Trader:
    def __init__(self, cfg: dict, risk, broker, journal, notifier=None, log=print):
        cap = cfg["capital"]
        self.cash = float(cap["start"])
        self.start_equity = self.cash
        self.ccy = str(cap.get("currency", "USDT"))
        self.sek_rate = float(cap.get("sek_per_unit", 0) or 0)
        self.names = dict(cfg.get("symbol_names", {}))
        self.timeframe = str(cfg.get("timeframe", ""))
        self.tfw = period_word(self.timeframe)
        self.fee_pct = float(cfg["risk"].get("fee_pct", 0.1))
        self.risk = risk
        self.broker = broker
        self.journal = journal
        self.notifier = notifier
        self.log = log
        self.strategies: dict = {}      # strategy name -> Strategy, for explanations (set by run.py)
        self.paper = True               # set by run.py; controls the "[paper]" tag
        self.bot_label = "Bot"          # set by run.py; used in public notifications
        self.positions: dict[tuple, Position] = {}
        self.last_prices: dict[str, float] = {}
        self.reserved_symbols: set[str] = set()   # symbols with a pending buy order (filled at the next open)
        self.trade_counter = 0
        self.stats: dict[str, int] = {"daily_halts": 0, "too_little_capital": 0, "blocked_signals": 0}

    # ---------- text helpers ----------
    def name(self, symbol: str) -> str:
        return self.names.get(symbol, symbol)

    def sek(self, amount: float) -> str:
        """Approximate SEK value, only when the account is kept in another currency."""
        if self.ccy == "SEK" or self.sek_rate <= 0:
            return ""
        return f" (~{fmt_num(amount * self.sek_rate, 0)} SEK)"

    def money(self, amount: float) -> str:
        if self.ccy == "SEK":
            return f"{fmt_num(amount, 0)} SEK"
        return f"{fmt_num(amount, 2)} {self.ccy}{self.sek(amount)}"

    def qty_text(self, symbol: str, qty: float) -> str:
        if float(qty).is_integer():
            return f"{int(qty)} shares"
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        return f"{qty:.6g} {base}"

    def label(self) -> str:
        return " [paper]" if self.paper else ""

    def strat_label(self, sname: str) -> str:
        s = self.strategies.get(sname)
        return s.label if s is not None else sname

    def why_buy(self, sname: str) -> str:
        s = self.strategies.get(sname)
        return s.why_buy(self.tfw) if s is not None else "entry condition met"

    def why_sell(self, sname: str, reason: str, pos: Position | None = None) -> str:
        if reason == "stop":
            level = f" ({fmt_num(pos.stop)})" if pos is not None else ""
            return f"stop-loss hit{level}: price fell to the level where the bot sells to cap the loss"
        if reason == "max_time":
            bars = pos.bars if pos is not None else "the maximum number of"
            return f"the position sat for {bars} {self.tfw}s without taking off, so the bot sells"
        if reason == "signal":
            s = self.strategies.get(sname)
            return s.why_sell(self.tfw) if s is not None else "exit condition met"
        return REASON_TEXT.get(reason, reason)

    # ---------- equity ----------
    def equity(self, prices: dict | None = None) -> float:
        p = dict(self.last_prices)
        if prices:
            p.update(prices)
        return self.cash + sum(pos.qty * p.get(pos.symbol, pos.entry_price) for pos in self.positions.values())

    def n_open(self) -> int:
        held = {p.symbol for p in self.positions.values()}
        return len(self.positions) + len(self.reserved_symbols - held)

    def has_position_in(self, symbol: str) -> bool:
        return symbol in self.reserved_symbols or any(p.symbol == symbol for p in self.positions.values())

    # ---------- signals ----------
    def evaluate_bar(self, key: tuple, bar: dict, strat) -> list[Intent]:
        """Called once per CLOSED bar for every (symbol, strategy)."""
        symbol, sname = key
        close = float(bar["close"])
        atr = float(bar.get("atr") or 0.0)
        self.last_prices[symbol] = close
        pos = self.positions.get(key)
        if pos is not None:
            pos.bars += 1
            if pos.trail_atr > 0 and atr > 0:
                pos.highest_close = max(pos.highest_close, close)
                new_stop = pos.highest_close - pos.trail_atr * atr
                if new_stop > pos.stop:
                    pos.stop = new_stop
            if bar["exit"]:
                return [Intent("sell", key, reason="signal")]
            if pos.max_hold_bars and pos.bars >= pos.max_hold_bars:
                return [Intent("sell", key, reason="max_time")]
            return []
        if not bar["entry"] or atr <= 0:
            return []
        ok, _why = self.risk.can_open(self.n_open())
        if not ok or self.has_position_in(symbol):
            self.stats["blocked_signals"] += 1
            return []
        stop = close - strat.stop_atr * atr
        if stop <= 0:
            return []
        quote = self.risk.position_quote(self.equity(), self.cash, close, stop, self.fee_pct)
        if quote <= 0:
            self.stats["too_little_capital"] += 1
            return []
        return [Intent("buy", key, quote=quote, stop=stop, atr=atr)]

    def stop_intents(self, symbol: str, price: float) -> list[Intent]:
        return [Intent("sell", key, reason="stop", stop=pos.stop)
                for key, pos in self.positions.items()
                if pos.symbol == symbol and price <= pos.stop]

    # ---------- bookkeeping ----------
    def apply_buy(self, intent: Intent, fill, strat) -> Position:
        symbol, sname = intent.key
        self.cash -= fill.quote
        self.trade_counter += 1
        self.reserved_symbols.discard(symbol)
        stop = fill.price - strat.stop_atr * intent.atr
        pos = Position(symbol=symbol, strategy=sname, qty=fill.qty, entry_price=fill.price, entry_ms=int(fill.ts),
                       cost=fill.quote, entry_fee=fill.fee, stop=stop, trail_atr=strat.trail_atr,
                       highest_close=fill.price, max_hold_bars=strat.max_hold_bars, bars=0,
                       trade_id=self.trade_counter)
        self.positions[intent.key] = pos
        self.last_prices[symbol] = fill.price
        eq = self.equity()
        risk_amount = (fill.price - stop) * fill.qty
        msg = (f"{self.qty_text(symbol, fill.qty)} at {fmt_num(fill.price)} = {self.money(fill.quote)} incl. fees.\n"
               f"Why: {self.why_buy(sname)}.\n"
               f"The bot sells automatically if price falls to {fmt_num(stop)} "
               f"({(stop / fill.price - 1) * 100:+.1f}%), capping the loss at about {self.money(risk_amount)}.\n"
               f"Equity now: {self.money(eq)}.")
        title = f"{self.name(symbol)} bought{self.label()} [{self.strat_label(sname)}]"
        self.log(f"[{fmt_ms(fill.ts)}] BUY {self.name(symbol)}: " + msg.replace("\n", " "))
        if self.notifier:
            self.notifier.send(title, msg, tags=["chart_with_upwards_trend"])
            self.notifier.send_public(f"BUY {self.name(symbol)} [{self.strat_label(sname)}]",
                                      f"{self.bot_label}: {msg}", tags=["chart_with_upwards_trend"])
        return pos

    def apply_sell(self, key: tuple, fill, reason: str) -> dict:
        pos = self.positions.pop(key)
        self.cash += fill.quote
        self.last_prices[pos.symbol] = fill.price
        pnl = fill.quote - pos.cost
        pnl_pct = pnl / pos.cost * 100 if pos.cost else 0.0
        trade = {
            "id": pos.trade_id, "symbol": pos.symbol, "strategy": pos.strategy,
            "entry_time": fmt_ms(pos.entry_ms), "entry_ms": int(pos.entry_ms), "entry_price": round(pos.entry_price, 6),
            "qty": pos.qty, "cost": round(pos.cost, 4),
            "exit_time": fmt_ms(fill.ts), "exit_ms": int(fill.ts), "exit_price": round(fill.price, 6),
            "proceeds": round(fill.quote, 4), "pnl": round(pnl, 4), "pnl_pct": round(pnl_pct, 3),
            "fees": round(pos.entry_fee + fill.fee, 4), "reason": reason, "bars": pos.bars,
        }
        self.journal.append_trade(trade)
        eq = self.equity()
        outcome = "Profit" if pnl > 0 else "Loss"
        msg = (f"{self.qty_text(pos.symbol, pos.qty)} at {fmt_num(fill.price)}. {outcome} {self.money(abs(pnl))} "
               f"({pnl_pct:+.2f}%) after fees, bought at {fmt_num(pos.entry_price)}, held {pos.bars} {self.tfw}s.\n"
               f"Why: {self.why_sell(pos.strategy, reason, pos)}.\n"
               f"Equity now: {self.money(eq)} ({(eq / self.start_equity - 1) * 100:+.1f}% since start).")
        title = f"{self.name(pos.symbol)} sold {pnl_pct:+.1f}%{self.label()}"
        self.log(f"[{fmt_ms(fill.ts)}] SELL {self.name(pos.symbol)}: " + msg.replace("\n", " "))
        if self.notifier:
            tags = ["white_check_mark" if pnl > 0 else "x"]
            self.notifier.send(title, msg, tags=tags)
            self.notifier.send_public(f"SELL {self.name(pos.symbol)} {pnl_pct:+.1f}%", f"{self.bot_label}: {msg}", tags=tags)
        return trade

    def close_all(self, ts: int, prices: dict, reason: str) -> None:
        for key in list(self.positions):
            pos = self.positions[key]
            ref = prices.get(pos.symbol, self.last_prices.get(pos.symbol, pos.entry_price))
            try:
                fill = self.broker.sell(pos.symbol, pos.qty, ref, ts)
                self.apply_sell(key, fill, reason)
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not close {key}: {e}")

    def mark(self, ts: int, prices: dict, record: bool = True) -> list[tuple[str, str]]:
        """Marks the account to market, records the equity curve and checks the risk limits."""
        self.last_prices.update(prices)
        eq = self.equity()
        if record:
            self.journal.append_equity({"time": fmt_ms(ts), "ms": int(ts),
                                        "equity": round(eq, 4), "cash": round(self.cash, 4)})
        events = self.risk.update_equity(eq)
        for kind, text in events:
            if kind == "daily_halt":
                self.stats["daily_halts"] += 1
            title = EVENT_TITLES.get(kind, kind)
            self.log(f"[{fmt_ms(ts)}] {title.upper()}: {text}")
            if self.notifier:
                self.notifier.send(f"{title}{self.label()}", text, priority=EVENT_PRIO.get(kind, 3),
                                   tags=["warning"] if kind in ("daily_halt", "kill_switch") else ["shield"])
        return events

    # ---------- save / restore (live) ----------
    def to_dict(self) -> dict:
        return {"cash": self.cash, "start_equity": self.start_equity, "trade_counter": self.trade_counter,
                "positions": {f"{k[0]}|{k[1]}": asdict(p) for k, p in self.positions.items()},
                "last_prices": self.last_prices, "stats": self.stats}

    def restore(self, d: dict) -> None:
        self.cash = float(d["cash"])
        self.start_equity = float(d.get("start_equity", self.start_equity))
        self.trade_counter = int(d.get("trade_counter", 0))
        self.positions = {tuple(k.split("|")): Position(**p) for k, p in d.get("positions", {}).items()}
        self.last_prices = {k: float(v) for k, v in d.get("last_prices", {}).items()}
        self.stats.update(d.get("stats", {}))
