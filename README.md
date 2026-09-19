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
- **Separate test and live modes** — a `MODE` constant in `futures/bot.py` selects the Binance
  testnet or the live account. Each mode has its own settings file and its own journal, and the two
  share one copy of the trading code, so behaviour cannot drift between them.

## Project structure

```
futures/                  the active system
  bot.py                  entry point: the polling loop, and MODE
  env.py                  MODE -> exchange, settings file, journal directory
  settings.py             Settings/Zone dataclasses and a validating load()
  client.py               builds the Binance UMFutures client; corrects clock drift
  market.py               exchange reads (one REST call per endpoint per tick)
  strategy.py             pure decision logic - no I/O, no client, no clock
  ladder.py               pure limit-order-ladder computation
  execution.py            order placement, cancellation, lot/tick rounding
  journal.py              JSONL tick and order logs
  config.py               API credentials - GITIGNORED, create it yourself
  notify.py               Telegram formatting and sending

  test/settings.json      testnet config      } one per mode,
  live/settings.json      live config         } re-read every tick
  test/logs/              testnet journals    } gitignored,
  live/logs/              live journals       } created on first write

tests/                    321 tests; conftest.py puts futures/ on sys.path
docs/superpowers/         design specs and implementation plans
binance/                  LEGACY Spot bot - out of scope, unused
```

`futures/` is **not a Python package.** Its modules import each other flatly (`import market`, not
`futures.market`), so run the bot and its scripts from inside `futures/`, or put that directory on
`PYTHONPATH`.

Three boundaries are deliberate and worth preserving:

- **`strategy.py` and `ladder.py` are pure.** Plain values in, a decision or an order set out. This is
  the seam a reinforcement-learning policy will later replace, and it is what makes them exhaustively
  testable without an exchange. Don't give them a client, a network call, or a clock.
- **Only `client.py` reads credentials.** `env.py` resolves modes without importing `config.py`, which
  keeps it — and its tests — working on a fresh clone where that gitignored file does not exist.
- **Only `bot.py` holds mutable state.** Every other module is stateless and takes its inputs as
  arguments.

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
3. Configure the settings file for the mode you intend to run — `futures/test/settings.json` or
   `futures/live/settings.json`. Each holds symbol, trend (`long`/`short`), leverage, alpha, exposure
   fraction, rebalance threshold, stop buffer, poll interval, rung spacing, liquidation buffer, and
   the ordered zone ladder (support/resistance pairs). These files are re-read every tick, so every
   field can be edited live without restarting the bot.

   There is deliberately **no exchange selector in these files.** Which account is traded is `MODE`
   in `futures/bot.py`, so a settings edit can never swap accounts underneath a running bot.

## Running

Run the bot from a directory where `futures/` modules can be imported as top-level modules — `futures/`
is not a package:

```
cd futures
python bot.py
```

### Choosing the account

Near the top of `futures/bot.py`:

```python
MODE = env.TEST   # Binance testnet - fake money, real order matching
# MODE = env.LIVE # the live account - REAL MONEY
```

Leave it on `env.TEST` and confirm correct behaviour on Binance's testnet before ever switching to
live trading. Switching requires editing this file and restarting — it is not a runtime setting, by
design. Starting in `live` prints a large banner and pushes a Telegram notice; it does not prompt,
so that an unattended restart is never left waiting for a keypress.

To check credentials and connectivity without placing any orders:

```
cd futures
python client.py         # testnet
python client.py live    # the live account
```

To verify your setup, run the test suite:

```
.viper/bin/python -m pytest tests/ -v
```

(on Windows the same interpreter is `.viper\Scripts\python.exe`; adjust the path if your virtual
environment lives elsewhere).
