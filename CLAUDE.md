# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

ViperTrading is an automated crypto trading bot for Binance USDⓈ-M Futures. It runs a zone-scaling
strategy: the operator supplies a direction (`trend`, long or short) and an ordered ladder of
support/resistance zones. Each tick the bot computes a target position size from how close price sits
to the active zone's favourable level (`target = max_notional * d ** alpha`, where `d` is the
normalised distance to that level), compares it to the actual position, and market-orders only the
difference. Scaling out needs no separate logic — as price nears the unfavourable edge of a zone, `d`
falls toward 0 and the target shrinks with it. Breaking out of a zone entirely flattens the position and
steps to the next zone in the ladder.

The active system lives in `futures/`:
- `futures/settings.py` — `Settings`/`Zone` dataclasses and a validating `load()`.
- `futures/settings.json` — runtime config (symbol, trend, leverage, alpha, exposure fraction,
  rebalance threshold, stop buffer, poll interval, testnet flag, zone ladder). Re-read every tick — see
  architecture notes below.
- `futures/client.py` — `build(testnet)` constructs the Binance `UMFutures` client; also measures the
  offset between this machine's clock and Binance's server clock at startup and patches
  `binance.api.get_timestamp` with it.
- `futures/market.py` — exchange reads: mark price, signed position amount, wallet balance, available
  balance, unrealized PnL, symbol filters (lot step, min qty, min notional), and setting leverage.
- `futures/strategy.py` — pure decision logic. `decide()` takes plain values and returns a `Decision`;
  see architecture notes for why this module has no I/O.
- `futures/execution.py` — quantity flooring to the lot step, exchange filter checks, and market-order
  placement.
- `futures/journal.py` — two JSONL logs under `futures/logs/` (gitignored): `ticks-YYYY-MM-DD.jsonl`
  (one line per loop iteration, including no-ops) and `orders-YYYY-MM-DD.jsonl` (one line per executed
  order, carrying a full environment snapshot at fill time).
- `futures/bot.py` — the polling loop (entry point). Owns only two pieces of mutable state across
  iterations: `active_index` (which zone is currently being worked) and `halted` (whether the
  HALT-left-the-ladder notice has already been printed).

`binance/` (`binance/main.py`, `binance/biat.py`) is the **legacy Spot bot** — a volatility-breakout
strategy for Spot XRP/USDT with Slack alerting. It is retained for reference but is **out of scope and
unused**: do not edit it expecting any effect on the running system, and do not treat its conventions
(e.g. Slack posting, the `"USDT"`-suffix symbol normalization) as applying to `futures/`.

## Setup

Dependencies are pinned in `requirements.txt` (now includes `pytest` as well as the Binance Futures
connector). Install them with:

```
pip install -r requirements.txt
```

A `futures/config.py` module is required at runtime but is not checked into the repo (it's gitignored).
It must define four names:
- `api_key`, `api_secret` — live Binance Futures API credentials
- `testnet_key`, `testnet_secret` — separate Binance Futures **testnet** API credentials

Live and testnet credentials are entirely separate and not interchangeable; `client.build(testnet)`
picks the pair to use based on the `testnet` flag in `settings.json`.

The legacy `binance/` bot has its own separate, also-gitignored `binance/config.py` (`api_key`,
`api_secret`, `slack_token`) — unrelated to `futures/config.py` and not needed to run the futures bot.

## Running

```
python futures/bot.py
```

Run from a directory where `futures/` modules can be imported as top-level modules (e.g. `cd futures`
and run `python bot.py`, or run with `futures/` on `PYTHONPATH`) — `futures/` is not a package and its
modules use flat imports (`import market`, `from settings import Zone`, etc.), not `futures.market`.

Tests: 58 tests live under `tests/`, run with:

```
.viper/Scripts/python.exe -m pytest tests/ -v
```

`tests/conftest.py` inserts `futures/` onto `sys.path` so the flat imports resolve during test
collection.

## Architecture notes

- **`strategy.py` is pure: no I/O, no client, no clock.** `decide()` takes plain values in and returns a
  `Decision` out. This purity is deliberate — it is the seam where a reinforcement-learning (PPO) policy
  will later replace the hand-written rules, and it is what makes the module exhaustively unit-testable
  without touching an exchange. Do not add a client argument, a network call, or a `datetime.now()` to
  this module; push any such need to `bot.py` and pass the result in as a value.
- **Sizing uses TOTAL wallet balance (`market.get_wallet_balance`), never available balance
  (`market.get_available_balance`).** Available balance shrinks as margin is consumed by an open
  position, which would shrink the computed target notional as the position fills and stall
  accumulation short of its intended size. `get_available_balance` exists and is logged for visibility,
  but must not feed `max_notional`.
- **Order quantities floor to the lot step, never round up** (`execution.quantity_for` uses
  `math.floor`). Rounding up would let the bot exceed its own exposure cap on the last partial step of a
  fill — flooring is the only direction that cannot overshoot.
- **`settings.json` is re-read every tick** (`bot.py`'s loop calls `settings.load()` each iteration).
  This lets trend, leverage, and the zone ladder change without restarting the bot. An invalid edit
  (caught as `ValueError`/`KeyError`/`OSError`) is logged to the tick journal as a `config_error` and
  otherwise ignored — the prior valid `Settings` stay in force. Never make a settings-load failure
  fatal to the loop.
- **Zones are contiguous** (one zone's `support` equals the next zone's `resistance`), so `stop_buffer`
  doubles as a hysteresis dead band around zone boundaries (`select_zone` in `strategy.py`). Without it,
  price sitting exactly on a shared boundary would flip the target between maximum and flat on every
  tick, flapping full-size positions in and out. Do not remove `stop_buffer` or treat zone edges as
  exact thresholds.
- **Console output must be ASCII-only.** This machine's console codepage is `cp949`, which cannot
  encode characters like the em-dash. The startup banner in `bot.py` prints before any error handling
  exists in the loop, so a non-ASCII character there crashes the bot before it starts, with no handler
  to catch it. Keep all `print()` output in `bot.py` (and anywhere else console output is added)
  restricted to ASCII.
- **The client patches `binance.api.get_timestamp`, not `binance.lib.utils.get_timestamp`**
  (`client.py`'s `sync_time`). `api.py` imports `get_timestamp` by name (a from-import), so rebinding the
  name in its original source module has no effect on the code path that actually runs — only patching
  the imported reference in `binance.api` works. This matters because the local clock runs measurably
  ahead of Binance's server clock, and Binance rejects a signed request whose timestamp is in the
  future with error -1021; the offset is measured once at startup against a public endpoint and applied
  to every subsequent timestamp.
- `bot.py` holds only `active_index` and `halted` as mutable loop state; `market.py`, `strategy.py`,
  `execution.py`, and `journal.py` are stateless and take all inputs as arguments — don't reintroduce
  module-level mutable state into them.
