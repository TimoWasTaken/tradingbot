"""Brokers. PaperBroker simulates fills with fees and slippage, BinanceBroker places real orders."""
from __future__ import annotations

import hashlib
import hmac
import math
import time
import urllib.parse
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

import requests


@dataclass
class Fill:
    price: float   # average fill price
    qty: float     # units (coins or shares) owned after the buy
    quote: float   # money that left the account (buy, incl. fee) or came in net (sell)
    fee: float     # fee in the account currency
    ts: int


class PaperBroker:
    """Simulated broker. min_fee = minimum commission per order, whole_shares = integer share counts only."""
    name = "paper"

    def __init__(self, fee_pct: float = 0.1, slippage_pct: float = 0.05,
                 min_fee: float = 0.0, whole_shares: bool = False):
        self.fee = fee_pct / 100.0
        self.slip = slippage_pct / 100.0
        self.min_fee = float(min_fee)
        self.whole_shares = bool(whole_shares)

    def _fee(self, gross: float) -> float:
        return max(gross * self.fee, self.min_fee)

    def buy(self, symbol: str, quote_amount: float, ref_price: float, ts: int) -> Fill | None:
        """Buys for quote_amount including the fee. Returns None if that does not cover a single share."""
        price = ref_price * (1 + self.slip)
        if self.whole_shares:
            qty = math.floor(quote_amount / (price * (1 + self.fee)))
            while qty > 0 and qty * price + self._fee(qty * price) > quote_amount:
                qty -= 1
            if qty <= 0:
                return None
            gross = qty * price
            fee = self._fee(gross)
            return Fill(price, float(qty), gross + fee, fee, int(ts))
        fee = self._fee(quote_amount)
        qty = (quote_amount - fee) / price
        if qty <= 0:
            return None
        return Fill(price, qty, quote_amount, fee, int(ts))

    def sell(self, symbol: str, qty: float, ref_price: float, ts: int) -> Fill:
        price = ref_price * (1 - self.slip)
        gross = qty * price
        fee = self._fee(gross)
        return Fill(price, qty, gross - fee, fee, int(ts))


class BinanceBroker:
    name = "binance"

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, fee_pct: float = 0.1):
        if not api_key or not api_secret or "PASTE" in api_key.upper():
            raise ValueError("API key missing. Create secrets.json (copy secrets.example.json).")
        self.key = api_key
        self.secret = api_secret.encode()
        self.base = "https://testnet.binance.vision" if testnet else "https://api.binance.com"
        self.fee = fee_pct / 100.0
        self._info: dict[str, dict] = {}

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = dict(params or {})
        url = self.base + path
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            query = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
            url = f"{url}?{query}&signature={sig}"
            params = None
        r = requests.request(method, url, params=params, headers={"X-MBX-APIKEY": self.key}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Binance returned {r.status_code}: {r.text[:300]}")
        return r.json()

    def info(self, symbol: str) -> dict:
        if symbol not in self._info:
            data = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
            s = data["symbols"][0]
            filters = {f["filterType"]: f for f in s["filters"]}
            notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
            self._info[symbol] = {
                "base": s["baseAsset"], "quote": s["quoteAsset"],
                "step": Decimal(filters["LOT_SIZE"]["stepSize"]),
                "min_qty": Decimal(filters["LOT_SIZE"]["minQty"]),
                "min_notional": float(notional.get("minNotional", 5.0)),
            }
        return self._info[symbol]

    def round_qty(self, symbol: str, qty: float) -> Decimal:
        step = self.info(symbol)["step"]
        return (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step

    def balances(self) -> dict[str, float]:
        acc = self._request("GET", "/api/v3/account", signed=True)
        return {b["asset"]: float(b["free"]) for b in acc["balances"] if float(b["free"]) > 0}

    def balance(self, asset: str) -> float:
        return self.balances().get(asset, 0.0)

    def price(self, symbol: str) -> float:
        return float(self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])

    def buy(self, symbol: str, quote_amount: float, ref_price: float, ts: int) -> Fill:
        res = self._request("POST", "/api/v3/order", {
            "symbol": symbol, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": f"{quote_amount:.2f}", "newOrderRespType": "FULL",
        }, signed=True)
        return self._parse(res, symbol, "BUY", ts)

    def sell(self, symbol: str, qty: float, ref_price: float, ts: int) -> Fill:
        info = self.info(symbol)
        free = self.balance(info["base"])
        q = self.round_qty(symbol, min(qty, free))
        if q <= 0 or q < info["min_qty"]:
            raise RuntimeError(f"Too little {info['base']} to sell: {q} (free {free})")
        res = self._request("POST", "/api/v3/order", {
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "quantity": format(q.normalize(), "f"), "newOrderRespType": "FULL",
        }, signed=True)
        return self._parse(res, symbol, "SELL", ts)

    def _parse(self, res: dict, symbol: str, side: str, ts: int) -> Fill:
        info = self.info(symbol)
        executed = float(res.get("executedQty", 0) or 0)
        quote = float(res.get("cummulativeQuoteQty", 0) or 0)
        if executed <= 0 or quote <= 0:
            raise RuntimeError(f"Order was not filled: {res}")
        price = quote / executed
        qty = executed
        fee_quote = 0.0
        fee_in_quote_asset = 0.0
        for f in res.get("fills", []):
            comm = float(f.get("commission", 0) or 0)
            asset = f.get("commissionAsset", "")
            if asset == info["base"]:
                qty -= comm
                fee_quote += comm * price
            elif asset == info["quote"]:
                fee_in_quote_asset += comm
                fee_quote += comm
            else:  # e.g. BNB: estimate
                fee_quote += quote * self.fee
        if side == "BUY":
            return Fill(price, qty, quote, fee_quote, int(ts))
        return Fill(price, executed, quote - fee_in_quote_asset, fee_quote, int(ts))
