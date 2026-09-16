# Zone-scaling Futures bot — design

**Date:** 2026-09-16
**Status:** approved, pending implementation plan
**Branch:** `develop`

## Goal

An autonomous USDⓈ-M Futures trading bot ("Viper") that runs unattended on a
server. The operator supplies a market view — a direction and a ladder of
support/resistance zones — and the bot sizes positions continuously based on
where price sits within the active zone.

This is deliberately a stepping stone. The sizing decision is isolated behind a
pure function so a PPO policy can replace it later without touching the loop,
execution, or risk machinery. The decision log doubles as the training dataset
for that work.

## Scope

**In:** single-symbol trading loop, continuous position targeting, zone
laddering with stop-and-step, market-order execution, hot-reloadable runtime
config, decision and order logging, testnet support.

**Out:** the Omen frontend, any HTTP/API surface, RL training, multi-symbol
trading, backtesting over historical data. The legacy Spot code in `binance/`
is untouched and out of scope.

## Core model: continuous position targeting

The bot does not place and manage resting orders. Each tick it computes a
*target position size* from price's location inside the active zone, compares
that to the actual position, and market-orders only the difference.

For a zone with support `S` and resistance `R`, with price clamped to `[S, R]`:

```
trend=long:   d = (R - price) / (R - S)      # 0 at resistance, 1 at support
trend=short:  d = (price - S) / (R - S)      # 0 at support,    1 at resistance

target_notional = max_notional * (d ** alpha)
max_notional    = wallet_balance * leverage * exposure_fraction
```

`wallet_balance` is **total** wallet balance, not available balance. This is
load-bearing: available balance falls as margin is consumed, so sizing off it
would shrink `max_notional` as the position fills, which shrinks the target,
which stalls accumulation short of the intended size — a feedback loop that
silently caps the strategy well below its configured maximum.

`alpha` shapes the curve; at `alpha=2.0` the position is ~25% of maximum
mid-zone and most size commits in the final quarter approaching the favourable
level.

Scaling out needs no separate logic: as price moves toward the unfavourable
edge, `d` falls toward 0, the target shrinks, and the reconciler sells the
difference. At the unfavourable edge the target is exactly 0 — flat.

### Signed notional

Positions are tracked as *signed* notional: positive is long, negative short.

```
target_signed = +target_notional   if trend == long
                -target_notional   if trend == short

current_signed = position_amt * mark_price     # position_amt is signed

delta = target_signed - current_signed
delta > 0  ->  BUY    |delta| worth
delta < 0  ->  SELL   |delta| worth
```

One expression covers opening, adding, trimming, and closing in both
directions.

## Zones and invalidation

`zones` is an ordered list of `{support, resistance}` pairs, highest first.
The active zone is whichever contains the current price.

Each tick, in order:

1. **Stop-out check.** If holding a position and price has broken past the
   active zone's favourable edge by `stop_buffer` — below `support * (1 -
   stop_buffer)` when `trend=long`, above `resistance * (1 + stop_buffer)` when
   `trend=short` — flatten at market immediately and log a stop-out.
2. **Zone selection.** Select the zone containing price.
   - No zone contains price, and price has moved past the ladder's **adverse
     end** — below the lowest support when `trend=long`, above the highest
     resistance when `trend=short` → flatten and **halt**, with an alert.
   - No zone contains price, and price has moved past the ladder's
     **favourable end** — above the highest resistance when `trend=long`, below
     the lowest support when `trend=short` → hold flat and idle; resume when
     price re-enters a zone.
   - Price sits in a gap between two non-adjacent zones → hold flat and idle.
3. **Size and reconcile.** Compute `d`, derive the target, reconcile via
   market order.

Stepping to the next zone is a consequence of reselection, not a separate
mechanism: once flattened, the next tick finds price inside the adjacent zone
— the one below for `trend=long`, above for `trend=short` — and begins
accumulating there.

## Execution

Market orders only, sized to the reconciliation delta.

Quantity is derived as `|delta| / mark_price`, rounded **down** to the
symbol's `stepSize`, and validated against `minQty` and `MIN_NOTIONAL`.
Rounding down guarantees the bot never overshoots its own exposure cap.

**Churn guard.** An order is only sent when `|delta|` exceeds *both*:

- `rebalance_threshold * max_notional` (default 5%), and
- the exchange `MIN_NOTIONAL` (currently $20 on ETHUSDT)

Without this the bot would fire a tiny order on every price wiggle and bleed
out in fees.

Symbol filters (`stepSize`, `minQty`, `MIN_NOTIONAL`, `tickSize`) are fetched
from `exchange_info` at startup rather than hardcoded.

Leverage is set on the symbol at startup via the change-leverage endpoint to
match the configured value.

## Configuration

`futures/settings.json`, **re-read every iteration**. Changing `trend`,
`leverage`, or `zones` takes effect on the next tick with no restart — which is
also how Omen will drive the bot later: it writes this file.

```json
{
  "symbol": "ETHUSDT",
  "trend": "long",
  "leverage": 5,
  "alpha": 2.0,
  "exposure_fraction": 1.0,
  "rebalance_threshold": 0.05,
  "stop_buffer": 0.01,
  "poll_seconds": 5,
  "testnet": true,
  "zones": [
    { "support": 2350, "resistance": 2450 },
    { "support": 2250, "resistance": 2350 },
    { "support": 2150, "resistance": 2250 }
  ]
}
```

Settings are validated on every load. Invalid config (zones out of order,
overlapping, `support >= resistance`, unknown `trend`, non-positive
`leverage`) is rejected and the previous valid settings are retained, with the
error logged — a typo written while the bot is live must not crash it or, worse,
be acted upon.

Secrets stay in `futures/config.py` (gitignored), extended with separate
testnet credentials:

```python
api_key = "..."
api_secret = "..."
testnet_key = "..."
testnet_secret = "..."
```

## Logging

Two JSONL files, both append-only, under `futures/logs/`.

### Tick journal — `ticks.jsonl`

One line per loop iteration, recording what the bot saw and decided even when
it did nothing. This is the continuous state/action history PPO needs.

```
timestamp, symbol, mark_price, trend, leverage, active_zone_index,
support, resistance, d, max_notional, target_notional,
current_notional, delta, action, balance, unrealized_pnl
```

`action` is one of `hold`, `buy`, `sell`, `stop_out`, `halt`, `idle`.

### Order log — `orders.jsonl`

One line per *executed* order, capturing the full environment snapshot at the
moment of the fill. Deliberately self-contained and denormalised: reconstructing
the context of a trade must never require joining against the tick journal or
re-deriving state.

```
timestamp, order_id, client_order_id, symbol, side, reason,
quantity, fill_price, notional, commission, commission_asset,

# environment at execution
mark_price, trend, leverage, alpha, exposure_fraction,
active_zone_index, support, resistance, d,
max_notional, target_notional,
position_before, position_after,
balance, available_balance, unrealized_pnl
```

`reason` distinguishes `scale_in`, `scale_out`, `stop_out`, and `halt_flatten`,
so the log explains *why* each order happened, not just what was sent.

Both files rotate by day.

## Module layout

Under `futures/`:

| File | Responsibility |
|---|---|
| `config.py` | secrets only (gitignored, exists) |
| `settings.py` | load + validate `settings.json` |
| `client.py` | client construction; live/testnet switch (exists, extended) |
| `market.py` | reads — mark price, position, wallet + available balance, symbol filters |
| `strategy.py` | **pure** — distance, target sizing, zone selection, stop-out detection |
| `execution.py` | reconcile current → target; quantity rounding; filter validation |
| `journal.py` | tick and order JSONL writers |
| `bot.py` | the loop; wires the above together |

`strategy.py` takes plain values and returns plain values — no client, no I/O,
no clock. Everything genuinely decision-shaped lives there, which makes it both
exhaustively unit-testable and the single seam a PPO policy replaces.

`trading.py` (the current `get_balance` / `place_limit_order` /
`place_limit_order_test` scratch module) is superseded: its balance lookup moves
into `market.py` and its order placement into `execution.py`. It is removed
rather than left as a second, divergent path to the same endpoints.

## Testing

`strategy.py` carries real unit tests, written first, with no network access:

- `d` at both zone edges, mid-zone, and clamped beyond each edge
- `alpha` curve values at known points (0.25/0.5/0.75 → 6%/25%/56%)
- signed target for both `trend` values
- zone selection: inside, between zones, above all, below all
- stop-out triggering exactly at the buffer boundary, both directions
- delta sign and direction across open / add / trim / close / flip

`execution.py` gets unit tests against fake filters for quantity rounding,
min-notional rejection, and the churn guard.

The loop itself is validated by running against testnet, not by mocking the
exchange — mocked exchange behaviour would prove nothing about real fills.

## Initial values

Chosen by the operator on 2026-09-16:

| Setting | Value | Note |
|---|---|---|
| `symbol` | ETHUSDT | tick 0.01, step 0.001, min notional $20 |
| `trend` | long | |
| `leverage` | 5 | |
| `alpha` | 2.0 | convex — size concentrates near the level |
| `exposure_fraction` | 1.0 | full leveraged capacity |
| `rebalance_threshold` | 0.05 | |
| `stop_buffer` | 0.01 | |
| `poll_seconds` | 5 | |
| `testnet` | true | $1000 virtual balance |
| `zones` | **operator to supply** | seeded with a placeholder ladder around $2403 |

At $1000 / 5x / 1.0 the maximum position is $5000 (~2.08 ETH). With a $2.40
lot step that is ~2000 discrete size steps, so scaling is effectively smooth.

## Risk notes

- **The strategy fades into levels.** Its losing case is a sustained directional
  move straight through the ladder: it stops out at each zone in turn. Per-zone
  losses are bounded by `stop_buffer`, but consecutive breaks compound. Zone
  placement carries the strategy.
- **`exposure_fraction = 1.0` leaves no free-margin buffer at maximum size.**
  A gap through the stop could leave the account under-margined before the
  flatten fills. Acceptable for testnet validation; worth revisiting before
  live.
- **Market orders pay the spread and can slip**, particularly on a stop-out
  during a fast move. Accepted for v1 in exchange for execution simplicity.
- **The live account already holds an open position.** Testnet is entirely
  separate, so there is no interaction — but this bot must not be pointed at
  live while that position is open without first reconciling it.

## Future: the PPO seam

`strategy.py`'s signature — environment values in, target signed notional out —
is the interface a learned policy implements. The tick journal supplies
observations and actions; realised PnL from the order log supplies reward. No
part of the loop, execution, risk, or logging layer changes when the policy is
swapped in.
