# AutoTrading
Bitocin Automated Trading

## Viper

Viper is an automated crypto trading bot for Binance USDⓈ-M Futures, running a zone-scaling strategy
that sizes positions based on price proximity to operator-defined support/resistance zones.

- **Zone-scaling position sizing** — scales into and out of positions automatically as price moves
  through an ordered ladder of support/resistance zones, with no separate scale-out logic needed.
- **Live settings reload** — rereads its config every tick, so trend, leverage, and the zone ladder can
  be changed without restarting the bot.
- **Minimal-footprint market data** — one REST call per exchange endpoint per tick, staying well under
  Binance's rate limits even at fast poll intervals.
- **Clock drift correction** — measures and corrects for local/server clock offset at startup to avoid
  signed-request timestamp errors.
- **Automatic risk halt** — flattens the position and pauses when price breaks out of the configured
  zone ladder.
- **Telegram notifications** — pushes alerts for executed orders, halts, and refused/reloaded settings
  changes, without ever affecting trading logic.
- **Limit-order ladder for in-zone scaling** — rests scale-in/scale-out orders on the book as a
  discrete ladder instead of crossing the spread every tick, with a liquidation-aware cap on the
  accumulate side; exits (stop-outs, halts) still use market orders for certainty of execution.
- **JSONL audit trail** — records a tick-by-tick decision journal and a full order journal for
  after-the-fact analysis and future RL/PPO training.
- **Live and testnet modes** — switches between separate live and testnet Binance credentials via a
  config flag.

## Setup

1. Install dependencies (Python 3.10+ recommended):
   ```
   pip install -r requirements.txt
   ```
2. Create `futures/config.py` (gitignored, not included in this repo) with your Binance Futures API
   credentials. Live and testnet credentials are separate and not interchangeable:
   ```python
   api_key = "..."
   api_secret = "..."
   testnet_key = "..."
   testnet_secret = "..."
   ```
   Optionally add Telegram push notifications by also defining `telegram_token` and
   `telegram_chat_id`. Leaving both undefined or empty is a supported state — notifications simply
   stay off.
3. Configure `futures/settings.json` — symbol, trend (`long`/`short`), leverage, alpha, exposure
   fraction, rebalance threshold, stop buffer, poll interval, `testnet` flag, rung spacing, liquidation
   buffer, and the ordered zone ladder (support/resistance pairs). This file is re-read every tick, so
   most fields can be edited live without restarting the bot.

## Running

Run the bot from a directory where `futures/` modules can be imported as top-level modules — `futures/`
is not a package:

```
cd futures
python bot.py
```

Start with `"testnet": true` in `settings.json` and confirm correct behavior on Binance's testnet
before ever switching to live trading.

To verify your setup, run the test suite:

```
.viper/Scripts/python.exe -m pytest tests/ -v
```

(adjust the interpreter path to wherever your virtual environment lives).
