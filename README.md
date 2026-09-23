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
- **Arbitrage.** In an event whose outcomes are mutually exclusive (negRisk), the YES prices must sum to 1. When the
  YES bids sum to more than 1 the bot buys NO on those outcomes: the cost is below the guaranteed payout. Such gaps
  are rare and taken by fast bots; the paper bettor mostly measures how often a slow hourly scan still catches one.
- **Same-market arbitrage check (measurement).** Every 5 minutes the bot pulls the real order books of the 200 most
  traded Yes/No markets and tests the classic "YES ask + NO ask < 1" opportunity that public Polymarket bots
  advertise as guaranteed profit. It records how often the sum is below 1, how much size sits there and whether
  anything is left after the taker fee (`journal/polymarket/arb_check.csv`, hits in `arb_hits.csv`). A hit that is
  positive after fees becomes a paper bet through the normal all-or-nothing path. First result on 2026-09-22: 164
  markets, none below 1, median sum 1.010.

Paper fills pay Polymarket's taker fee where the market charges one (shares x rate x price x (1 - price); rate 0.07
on crypto, 0.05 on sports, 0.04 to 0.05 elsewhere, 0 on geopolitics) plus one tick of slippage. It also stores a daily
snapshot of every scanned market; `py polymarket.py research` later joins those with the resolutions to measure the
favorite-longshot bias on the bot's own data. Commands: `polymarket_scan.bat` (what it would bet on now, plus the
order-book check), `polymarket_start.bat`, `polymarket_report.bat`. Nothing is ever sent to Polymarket: no wallet, no
orders. Prediction markets are betting, not investing; they are zero-sum, may be unlicensed gambling where you live,
and winnings may be taxable.

### Crypto 15-minute markets: the speed test (paper)

Every 15 minutes Polymarket opens "Bitcoin Up or Down" and "Ethereum Up or Down" windows that pay UP if the Chainlink
price at the end is at or above the price at the start. The bots that demonstrably make money on Polymarket live
here: they watch the Binance spot price, which moves first, and buy the side Polymarket has not repriced yet.
Polymarket answered with a taker fee on these windows (0.07 x price x (1 - price) per share, 1.75 cents at 50c).

`bot/crypto15.py` measures how much of that edge is reachable at home-computer speed, polling public APIs every 5
seconds, with simulated money only:

- **Fair-value strategy.** From the Binance move since the window opened, the time left and the realised volatility of
  the last hour it computes the probability that the window ends UP. When a side's best ask is at least 4 cents
  below that probability after the taker fee and one tick of slippage, it buys the side (5% of a 100 USD paper
  bankroll, one bet per window and coin) and holds to resolution.
- **DipArb replay.** Public "Polymarket bot" repositories buy a side whose ask fell 15% within seconds and then try to
  buy the other side within 60 seconds so the pair costs 0.92 or less and pays 1. The watcher replays that rule with
  20 shares per dip and no bankroll limit, and logs every dip with what happened next (hedged, stopped out, or held
  to resolution), so the strategy's real frequency and cumulative result are on file. First 90 minutes
  (2026-09-22): 70 dips, 8 hedged, 56 stopped out, -92 USD. The "dips" are the market repricing on Binance moves,
  not panic.
- **Ticks.** Binance price, both order books and the model probability are written every 10 seconds
  (`journal/crypto15/ticks.csv`); `py crypto15.py research` joins them with the resolved windows and prints how well
  the market and the model were calibrated and whether buying on the model's signals would have paid.

Commands: `py crypto15.py scan`, `crypto15_start.bat`, `crypto15_report.bat`. Limits: the window's opening price is
the first Binance print the watcher sees after the window starts (Polymarket uses Chainlink at the exact start), and
five-second polling is orders of magnitude slower than the bots this measures against. That is the point: the journal
shows what is left for a slow participant after fees.

### Copy-trading test (paper)

The third idea public Polymarket bots sell is copying the leaderboard. `bot/copytrade.py` replays it honestly:
once a day it takes the overall and politics leaderboards (month and week), judges every wallet on its last 100
closed positions with the gate those bots advertise (60%+ win rate, profit factor 1.5+, 30+ closed positions, no
single position above 30% of the profit, plus: traded within a week and not mostly 5/15-minute crypto windows,
which cannot be copied in time), and follows up to 15. Every minute it polls their recent trades through the public
data API. A new buy is copied on paper at the live best ask plus one tick and the taker fee, 5% of a 100 USD
bankroll, unless the market ends within an hour, the price already moved more than 10% above theirs, or the
signal is older than 15 minutes; a sell by the same wallet in the same market closes the copy at the best bid.
Every signal is logged with the delay and the price premium (`journal/copytrade/signals.csv`), which is the number
those bots never publish. Commands: `py copytrade.py wallets` (who passes the gate now), `copytrade_start.bat`,
`copytrade_report.bat`.

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
Phones and earbuds were left out on purpose: thin margins, fakes, iCloud locks and stolen goods.

**Tradera: real sale prices and ending auctions.** With a free developer key from
[api.tradera.com/register](https://api.tradera.com/register) (`tradera_app_id` and `tradera_app_key` in `secrets.json`)
`bot/tradera.py` pulls ended Tradera listings for every watch and records the winning bids in `sold.csv`. The median
of those real sales replaces the Blocket asking-price median as the reference, so an alert says "usually sells for
2,140 kr (69 sales)" instead of "others ask 4,000 kr". It also checks the auctions ending within two hours and pushes
a note when the next bid is 40% or more under that value. The first run pulls eight pages of history per watch, so
most watches have a real reference within a minute; the report shows sell-through rates too, which tells you what
does not sell at all.

**Facebook Marketplace via Apify.** Facebook has no API and blocks logged-out search, so the deal finder never
talks to Facebook itself. With `"marketplace": {"enabled": true}` and an Apify API token in `secrets.json`
(`apify_token`, free account at apify.com with 5 USD of monthly credit), it asks Apify's maintained
`facebook-marketplace-scraper` for the newest listings of each watch that has a `marketplace_url` (copy the search
URL from your browser with your area, radius and "Date listed" sorting), a few times a day, and judges them with the
same filters and references as Blocket listings. Apify bills per listing returned (about half a cent), so keep it to
the heavy local categories and a handful of results per watch.

**Asking prices are not values.** Across the default watches the Tradera sale median was only 46 to 79% of the
Blocket asking median (typically about 55%). So when a watch has no sale data yet, the asking median is scaled by
`ask_to_sold_factor` (0.6) before it is used, and the margin in every alert is computed after `selling_cost_pct`
(12%, roughly Tradera's commission and payment fees). With only a few sales the bot uses the lower of the sale
median and the scaled asks. Pin `reference_price` on a watch if you know the market better than the data. The first pass only learns prices, so it does not flood you with old listings. Record the flips you actually
do with `py blocket.py flip --watch ... --bought ... --sold ...` and the report shows real profit next to the alerts.
Edit `config_blocket.json` to change watches, word filters, price bands or region (`py blocket.py locations` lists
the codes). Commands: `blocket_scan.bat`, `blocket_start.bat`, `blocket_report.bat`. Read Blocket's terms before
running this at a high rate or for anything commercial; the default is one request per watch every 10 minutes.

### Desktop edition of the deal finder (for people without Python)

`app/` is the sellable Windows edition, version 2.0: a local web app in an Edge/Chrome app window instead of a
tkinter form. `app/core.py` holds config, default watches and the background worker (Blocket + Tradera +
Marketplace through `bot/`), `app/server.py` is a 127.0.0.1-only HTTP server with a JSON API, `app/ui/index.html`
is the whole interface (Swedish: a three-step first-run guide, a Deals feed, watch cards with toggles, settings
with the advanced parts folded away, help), and `app/blocket_app.py` is the launcher: it starts the server, opens
the window, keeps running in the background with a tray icon, and re-opens the window if the .exe is started
again. Alerts are pushed in Swedish or English (`cfg["language"]`). 14-day trial, then an offline licence key
(`tools/keygen.py buyer@example.com` makes one from the `license_secret` in `secrets.json`; `build_app.bat`
bakes the same secret into the .exe as the git-ignored `app/_secret.py`). `build_app.bat` builds
`dist/BlocketDealFinder.exe` (~23 MB) and copies the buyer guide `app/GUIDE_SV.md` next to it. The sales page
`app/site/index.html` and the .exe are published by `publish.py` under https://timowastaken.github.io/tradingbot/dealfinder/
so buyers never see GitHub.

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
| `config_polymarket.json` | Polymarket paper bettor | `favorites`, `arbitrage`, `arb_check` (order-book check every 5 min), `capital` |
| `config_crypto15.json` | crypto 15-minute test | `coins`, `poll_seconds`, `fair` (min_edge, stake_pct), `diparb` (drop_pct, sum_target), `fee_rate` |
| `config_copytrade.json` | copy-trading test | `selection` (boards, max_wallets), `gate` (win rate, profit factor), `copy` (stake_pct, max_premium, skip_updown) |
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
polymarket.py   Polymarket paper bettor + order-book check      crypto15.py     crypto 15-minute speed test (paper)
copytrade.py    copy-trading test (leaderboard wallets, paper)
sportsarb.py    sports arbitrage measurement                    blocket.py      Blocket/Tradera/Marketplace deal finder
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
