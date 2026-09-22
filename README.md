# Tradingbot

Four rule-based trading bots, built as a learning project, running on simulated money against real prices:

| Bot | Market | Bars | What it does |
|---|---|---|---|
| **Crypto bot** | Binance spot, BTC and ETH vs USDT | 4 h | Trades itself (paper now, live possible with API keys) |
| **Swedish stocks bot** | 40 large caps, Nasdaq Stockholm | daily | Sends buy/sell signals; you place the orders (Avanza has no API) |
| **US stocks bot** | 40 US large caps | daily | Same, for New York |
| **Fund rotation bot** | 6 broad asset classes | monthly | Dual momentum between commission-free funds |

| **Polymarket paper bettor** | Prediction markets | hourly | Bets simulated money on two documented edges, measures if they are real |

Live track record (regenerated every evening): **https://timowastaken.github.io/tradingbot/**
Public signal feed: ntfy topic `tradingbot-signals-7k2m9` (see below).

> **Honest disclaimer.** This is a hobby project. Everything is paper trading. Nothing here is investment advice.
> In every backtest, simply buying and holding the index beat the bots. Their value, if any, is discipline, defined
> risk and smaller drawdowns, not higher returns. Never trade money you cannot afford to lose.

---

## How it works

Exactly the same code makes the decisions in backtests, paper trading and live trading (`bot/engine.py`). Rules that
keep the backtests honest: a signal on a closed bar is executed at the next bar's open, every fill is charged fees and
slippage, and stops are checked against each bar's low.

Three long-only strategies (`bot/strategies.py`), all requiring price above a long EMA:

- **Breakout**: close above the N-bar high (optionally on high volume), exit below the M-bar low. Trailing stop.
- **Trend**: fast EMA crosses above slow EMA, exit on the cross back down. Trailing stop.
- **Mean reversion**: close below the lower Bollinger band with RSI oversold, exit at the middle band or after a time limit.

Risk rules (`bot/risk.py`), identical in backtest and live: ATR-based stop per position, fixed percent risk per trade,
max open positions, daily-loss halt, kill switch at a maximum drawdown, and a *cautious mode* that halves position size
while equity is more than 10% below its peak.

The fund rotation bot (`bot/rotation.py`) is different: at each month-end close it ranks six asset classes by their
average 3/6/12-month return, holds the top two equal-weighted, and parks any slot that does not beat short-term bonds in
bonds. It is measured on US ETFs with 20 years of history and its signals name commission-free Avanza funds.

### Polymarket paper bettor

`bot/polymarket.py` scans Polymarket's public Gamma API every hour and bets a simulated 100 USD bankroll on two
edges that the academic literature documents:

- **Favorites.** A study of 588 million Polymarket trades (arXiv 2609.12878) found contracts bought at 90 cents or
  more paid about +0.3 to +1 cent per dollar more than their price implied (strongest in Politics and Crypto), while
  longshots under 10 cents lost 6 to 20 cents per dollar. Sports showed no such bias. The bot buys the side priced
  90 to 98.5 cents in liquid non-sports markets resolving within 45 days, 10% of bankroll per bet, capped per event
  and category, and holds to resolution.
- **Arbitrage.** When the asks of all mutually exclusive outcomes of an event sum to less than 1 (or YES ask + NO ask
  in a binary market), buying them all locks in the difference. Such gaps are rare and taken by fast bots; the paper
  bettor mostly measures how often a slow hourly scan still catches one.

It also stores a daily snapshot of every scanned market; `py polymarket.py research` later joins those with the
resolutions to measure the favorite-longshot bias on the bot's own data. Commands: `polymarket_scan.bat` (what it
would bet on now), `polymarket_start.bat`, `polymarket_report.bat`. Nothing is ever sent to Polymarket: no wallet, no
orders. Prediction markets are betting, not investing; they are zero-sum, may be unlicensed gambling where you live,
and winnings may be taxable.

### Sports arbitrage measurement

`bot/sportsarb.py` measures, without betting, how much "sure bet" money exists at Swedish-licensed bookmakers:
twice a day it pulls head-to-head odds for seven leagues from [The Odds API](https://the-odds-api.com) (free key,
500 credits a month, one credit per sport per scan), finds the best odds per outcome, and logs every event's margin
and every arbitrage (sum of 1/odds below 1) with the stake split for a given capital. Two sets are tracked: Swedish-
licensed books only (Unibet, Betsson, NordicBet, LeoVegas, Coolbet, Bet365, William Hill) and all books including
Pinnacle and the Betfair exchange as a reference. `sportsarb_report.bat` turns the log into arbitrages per week,
average and best profit, which bookmakers were involved, and an upper-bound SEK-per-week estimate. Put the key in
`secrets.json` as `odds_api_key`. Commands: `sportsarb_scan.bat`, `sportsarb_start.bat`, `sportsarb_report.bat`.

### Blocket deal finder

`bot/blocket.py` is the one tool here that competes with people instead of with bots. It polls Blocket's public
search JSON for a list of watches (Festool and Hilti tools, Bugaboo and Thule strollers, e-bikes, Weber grills,
Automower, Louis Poulsen and String design, Concept2 rowers, Nord keyboards, Fender guitars, camera lenses), learns
the going asking price per watch from the listings it sees (median of the last 200 that pass the word filters), and
pushes a notification the moment a new listing appears at least 30% under that level, or an existing one drops to
it, with an estimated resale margin. Brand watches with varied listings can use a hard `alert_below` price instead.
Phones and earbuds were left out on purpose: thin margins, fakes, iCloud locks and stolen goods. The first pass only learns prices, so it does not flood you with old listings. Record the flips you actually
do with `py blocket.py flip --watch ... --bought ... --sold ...` and the report shows real profit next to the alerts.
Edit `config_blocket.json` to change watches, word filters, price bands or region (`py blocket.py locations` lists
the codes). Commands: `blocket_scan.bat`, `blocket_start.bat`, `blocket_report.bat`. Read Blocket's terms before
running this at a high rate or for anything commercial; the default is one request per watch every 10 minutes.

### Desktop edition of the deal finder (for people without Python)

`app/blocket_app.py` wraps the same deal finder in a small window: watches with add/edit/remove, settings (ntfy
topic with a test button, interval, region, start with Windows, Swedish or English), and a live log. Settings and
the journal live in `%APPDATA%\BlocketDealFinder`. `build_app.bat` builds it into a single `dist\BlocketDealFinder.exe`
with PyInstaller (about 15 MB, no console window). The exe is unsigned, so Windows SmartScreen shows a warning on
first start; click "More info" then "Run anyway".

---

## Backtest results (hypothetical, computed after the fact)

| Bot | Period | Bot | Buy & hold |
|---|---|---|---|
| Crypto, 4 h, BTC + ETH | 4 years | +80 %, PF 1.44, max DD -20 % | BTC +269 % |
| Swedish stocks, 40 names, 5 slots | 10 years | +64 %, PF 1.4, max DD -15 % | OMXS30 +130 % |
| US stocks, trend only | 10 years | +70 %, PF 2.1, max DD -11 % | S&P 500 +257 % |
| Fund rotation, top 2 | 2006–2026 | +7.0 %/yr, max DD -25 % | S&P 500 +10.9 %/yr, DD -55 %; Sweden +5.5 %/yr, DD -68 % |

Fast timeframes (15 min to 2 h) lost money in every crypto backtest once fees were included. The fund rotation bot is
the only one where the trade-off is interesting: less return than US stocks, but less than half the drawdown.

---

## Setup

Requires Python 3.11+ on Windows (the `.bat` launchers use the `py` launcher; on other systems run the `.py` files).

1. `install.bat` (or `pip install -r requirements.txt`).
2. Copy `secrets.example.json` to `secrets.json` and put your private [ntfy](https://ntfy.sh) topic in `ntfy_topic`
   (install the ntfy app on your phone and subscribe to the same topic). Test with `py test_push.py`.
3. Adjust the configs if you like (see below), then start the bots you want:
   `start.bat`, `stocks_se_start.bat`, `stocks_us_start.bat`, `funds_start.bat`.
   Each opens a console window; close it or press Ctrl+C to stop. A bot resumes from `journal/<name>/state.json`
   when restarted. Delete that folder to start from zero.
4. Reports from the journals: `report.bat`, `stocks_se_report.bat`, `stocks_us_report.bat`, `funds_report.bat`.
5. Backtests: `backtest.bat`, `stocks_se_backtest.bat`, `stocks_us_backtest.bat`, `funds_backtest.bat`.
   Flags: `--days`, `--timeframe` (crypto), `--symbols`, `--strategy`, `--with-kill-switch`.

The bots must keep running: put shortcuts to the `_start.bat` files in the Windows Startup folder and disable sleep.
Stops are watched by the bot, not by the exchange, so a stopped bot means no stop.

### Configuration files

| File | Bot | Key settings |
|---|---|---|
| `config.json` | crypto | `mode` (`paper`/`live`), `symbols`, `timeframe`, `strategies`, `capital`, `risk` |
| `config_stocks_se.json` | Swedish stocks | watch list in `symbols` + `symbol_names`, `execution: next_open` |
| `config_stocks_us.json` | US stocks | same |
| `config_funds.json` | fund rotation | `universe`, `safe`, `top_n`, `lookbacks`, `fund_names` |
| `config_publish.json` | track-record page | GitHub user, repo, branch, public ntfy topic |

Common `risk` keys: `risk_per_trade_pct`, `max_position_pct`, `max_open_positions`, `max_daily_loss_pct`,
`max_drawdown_pct` (kill switch), `reduce_at_drawdown_pct` / `reduce_exit_drawdown_pct` / `reduce_factor`
(cautious mode), `fee_pct`, `min_fee`, `slippage_pct`, `min_notional`, `whole_shares`.

### Live trading (crypto only)

1. Paper trade for weeks first and read the journal.
2. Create a Binance API key with *trading* permission only (no withdrawals), optionally IP-restricted.
   Put key and secret in `secrets.json`. Try `"testnet": true` in `config.json` first (https://testnet.binance.vision).
3. Set `"mode": "live"` and `capital.start` to your actual balance. The bot prints your balance and a warning on start.
   Never switch modes with open positions.

---

## Notifications

Every buy, sell, signal, risk event and a daily report go to your private ntfy topic. The same trades, signals and fund
decisions (not the risk events or daily reports) also go to the public topic in `ntfy.public_topic`, each with a
disclaimer footer and a link to the track-record page. Free ntfy topics are not access-controlled: anyone who knows the
name can read *and write*, so treat unexpected messages as noise or move the feed to a Telegram channel if it grows.

## Publishing the track record

`publish.py` builds `public/index.html` plus CSV copies of the journals from `journal/` and pushes them to the
`gh-pages` branch of the repo in `config_publish.json`. It publishes journals only, never code, keys or the private
topic. Run it with `publish.bat` or schedule it (Windows Task Scheduler, full path to `python.exe`, daily).

## Project layout

```
bot/            the package: config, data (Binance), data_stocks (Yahoo), indicators, strategies, risk,
                broker (paper + Binance), engine, journal, backtest, report, rotation, notify
run.py          runs a crypto or stock bot          backtest.py     backtests a crypto or stock bot
rotation.py     fund rotation bot (backtest/run/report)
report.py       journal report                      publish.py      track-record page -> GitHub Pages
test_push.py    ntfy test                           *.bat           double-click launchers (Windows)
journal/        per-bot state and CSV journals (not committed)      data/  cached candles (not committed)
```

## Data sources and limits

Crypto candles come from Binance's public API. Stock and ETF prices come from Yahoo Finance via `yfinance`, which is
unofficial, roughly 15 minutes delayed, and not licensed for commercial use. Stops on the stock bots are evaluated
against those delayed prices. Fund rotation ignores SEK/USD currency moves.

## License

Source published for transparency. No license granted yet; all rights reserved until one is chosen.
