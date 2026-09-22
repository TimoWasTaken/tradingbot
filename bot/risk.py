"""Risk rules: position sizing, cautious mode, daily-loss halt and kill switch. Same rules in backtest and live."""
from __future__ import annotations


class RiskManager:
    def __init__(self, cfg_risk: dict, start_equity: float):
        self.risk_per_trade_pct = float(cfg_risk.get("risk_per_trade_pct", 2.0))
        self.max_position_pct = float(cfg_risk.get("max_position_pct", 95.0))
        self.max_open_positions = int(cfg_risk.get("max_open_positions", 1))
        self.max_daily_loss_pct = float(cfg_risk.get("max_daily_loss_pct", 5.0))
        self.max_drawdown_pct = float(cfg_risk.get("max_drawdown_pct", 25.0))
        self.min_notional = float(cfg_risk.get("min_notional", 5.0))
        # cautious mode: smaller positions while equity is in a drawdown
        self.reduce_at_dd = float(cfg_risk.get("reduce_at_drawdown_pct", 10.0))
        self.reduce_exit_dd = float(cfg_risk.get("reduce_exit_drawdown_pct", 5.0))
        self.reduce_factor = float(cfg_risk.get("reduce_factor", 0.5))
        self.cautious = False
        self.cautious_count = 0
        self.current_dd = 0.0
        self.peak_equity = float(start_equity)
        self.max_dd = 0.0
        self.day_key: int | None = None
        self.day_start_equity = float(start_equity)
        self.halted_today = False
        self.killed = False
        self.kill_reason = ""
        self.daily_halts = 0

    # ---------- save / restore ----------
    def to_dict(self) -> dict:
        return {"peak_equity": self.peak_equity, "max_dd": self.max_dd, "day_key": self.day_key,
                "day_start_equity": self.day_start_equity, "halted_today": self.halted_today,
                "killed": self.killed, "kill_reason": self.kill_reason, "daily_halts": self.daily_halts,
                "cautious": self.cautious, "cautious_count": self.cautious_count}

    def restore(self, d: dict) -> None:
        self.peak_equity = float(d.get("peak_equity", self.peak_equity))
        self.max_dd = float(d.get("max_dd", 0.0))
        self.day_key = d.get("day_key")
        self.day_start_equity = float(d.get("day_start_equity", self.day_start_equity))
        self.halted_today = bool(d.get("halted_today", False))
        self.killed = bool(d.get("killed", False))
        self.kill_reason = str(d.get("kill_reason", ""))
        self.daily_halts = int(d.get("daily_halts", 0))
        self.cautious = bool(d.get("cautious", False))
        self.cautious_count = int(d.get("cautious_count", 0))

    # ---------- rules ----------
    def new_time(self, ts_ms: int, equity: float) -> bool:
        """Called every cycle. Resets the daily tally on a new (UTC) day. Returns True on a new day."""
        key = int(ts_ms) // 86_400_000
        if key == self.day_key:
            return False
        self.day_key = key
        self.day_start_equity = float(equity)
        self.halted_today = False
        return True

    def update_equity(self, equity: float) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        if equity > self.peak_equity:
            self.peak_equity = equity
        if self.peak_equity > 0:
            dd = (equity / self.peak_equity - 1.0) * 100.0
            self.max_dd = min(self.max_dd, dd)
        else:
            dd = 0.0
        self.current_dd = dd
        if not self.halted_today and self.day_start_equity > 0:
            day_pct = (equity / self.day_start_equity - 1.0) * 100.0
            if day_pct <= -self.max_daily_loss_pct:
                self.halted_today = True
                self.daily_halts += 1
                events.append(("daily_halt",
                               f"The bot has lost {abs(day_pct):.1f}% today, more than the {self.max_daily_loss_pct:.0f}% "
                               f"limit. No new buys until tomorrow. Existing positions keep their stops."))
        if self.reduce_at_dd > 0 and 0 < self.reduce_factor < 1:
            if not self.cautious and dd <= -self.reduce_at_dd:
                self.cautious = True
                self.cautious_count += 1
                events.append(("cautious",
                               f"Equity is {abs(dd):.1f}% below its peak. Entering cautious mode: new positions at "
                               f"{self.reduce_factor * 100:.0f}% of normal size until the drawdown is under "
                               f"{self.reduce_exit_dd:.0f}%."))
            elif self.cautious and dd >= -self.reduce_exit_dd:
                self.cautious = False
                events.append(("normal",
                               f"Drawdown is now only {abs(dd):.1f}% from the peak. Cautious mode ended, "
                               f"normal position size again."))
        if not self.killed and dd <= -self.max_drawdown_pct:
            self.killed = True
            self.kill_reason = (f"Equity is {abs(dd):.1f}% below its peak, more than the "
                                f"{self.max_drawdown_pct:.0f}% limit.")
            events.append(("kill_switch", self.kill_reason + " The bot sells everything and shuts itself down. "
                                                            "See the README for how to restart."))
        return events

    def can_open(self, n_open: int) -> tuple[bool, str]:
        if self.killed:
            return False, "kill switch active"
        if self.halted_today:
            return False, "daily halt active"
        if n_open >= self.max_open_positions:
            return False, "max open positions reached"
        return True, ""

    def position_quote(self, equity: float, cash: float, price: float, stop_price: float, fee_pct: float) -> float:
        """How much to buy for. Based on the distance to the stop: we want to lose at most
        risk_per_trade_pct of equity if the stop is hit. In cautious mode only a fraction of normal size."""
        if price <= 0 or stop_price <= 0 or stop_price >= price:
            return 0.0
        stop_dist = (price - stop_price) / price
        risk_quote = equity * self.risk_per_trade_pct / 100.0
        quote = risk_quote / stop_dist
        quote = min(quote, equity * self.max_position_pct / 100.0, cash * (1 - fee_pct / 100.0) * 0.999)
        if self.cautious:
            reduced = quote * self.reduce_factor
            # with tiny capital half size can fall below the exchange minimum:
            # trade the minimum instead of not trading at all
            quote = reduced if reduced >= self.min_notional else min(quote, self.min_notional)
        if quote < self.min_notional:
            return 0.0
        return round(quote, 2)
