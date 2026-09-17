# Limit-order ladder execution — design

**Date:** 2026-09-17
**Status:** approved, pending implementation plan
**Branch:** `develop`

## Goal

Replace market-order execution for position accumulation and in-zone
scale-out with resting limit orders, to save trading commission (maker fees
instead of taker) without changing the underlying continuous-target sizing
model in `strategy.py`. Guaranteed-exit paths (`STOP_OUT`, `HALT_FLATTEN`, and
the off-ladder dead-band flatten) keep using market orders unchanged, because
a resting order that never fills while price runs away is exactly the failure
mode those paths exist to prevent.

This is additive to the existing zone-scaling design
(`2026-09-16-zone-scaling-futures-bot-design.md`), not a replacement of it.
`strategy.decide()` is untouched and still computes the continuous target that
routes between exit paths and the new ladder path — it remains the seam a
future PPO policy replaces.

## Scope

**In:** a discretized "ladder" of resting limit orders per active zone that
approximates the existing continuous target curve; fill detection via
polling; a liquidation-aware safety brake that shrinks unfilled buy-side
rungs; a switch to ISOLATED margin mode; the config, journal, and
notification additions the ladder needs.

**Out:** websocket user data stream fill detection (see Future Extensions),
multi-symbol ladders, backtesting, any change to `strategy.decide()`'s
contract or to the market-order exit paths.

## Why market orders stay for exits

`STOP_OUT` fires when the active zone changes while holding a position —
per the existing design, this is routinely a large position-*increasing*
order in the new zone's direction, not necessarily a flatten, and the
position on the *old* zone must close immediately so the new zone's ladder
starts from zero. `HALT_FLATTEN` fires when price leaves the ladder in the
thesis-invalidating direction. Both are safety-critical, guaranteed exits —
a limit order resting unfilled while price keeps moving away is the
opposite of what either path needs. The same reasoning applies to the
existing "dead band between two non-adjacent zones" flatten (reason
`SCALE_OUT`, `zone_index is None`): there is no active zone to rest a
ladder in, so it stays a market order too.

The only case the ladder replaces is `SCALE_IN`/`SCALE_OUT` **inside an
active zone** — accumulating toward, or trimming back from, the continuous
target as price moves within `[support, resistance]`.

## Module layout

| File | Change |
|---|---|
| `ladder.py` (new) | **Pure.** Rung table generation, buy/sell split by current price, liquidation-aware buy-side cap, rung validation, diffing a desired rung table against open orders into a cancel/place plan. |
| `market.py` | + `get_open_orders`, `liquidation_price_from` (extracts the existing `liquidationPrice` field from the position-risk payload `get_snapshot` already fetches), `get_leverage_brackets` + a maintenance-margin-tier extractor, `set_margin_type`. |
| `execution.py` | + `place_limit_order`, `cancel_orders`. `execute()` (market) is unchanged and remains the only path for `STOP_OUT`/`HALT_FLATTEN`/dead-band exits. |
| `bot.py` | + mutable ladder state: tracked rung→order-id map for the active zone, and a "settling" flag/snapshot for the fill-stabilization wait. Routes in-zone `SCALE_IN`/`SCALE_OUT` to the ladder path instead of `execution.execute()`. |
| `settings.py`/`settings.json` | + `rung_spacing_pct`, `liquidation_buffer_pct` (both hot-reloadable). |
| `journal.py` | No interface change; records gain new fields (below). |
| `notify.py` | + one new event: the liquidation brake engaging. |

`ladder.py` is kept separate from `strategy.py` rather than folded into it.
`strategy.decide()`'s contract — plain values in, one continuous
`Decision` out — is the interface a learned policy will implement later;
that policy will still output a continuous target, and `ladder.py` is what
turns *that* into discrete resting orders. Mixing the two would put
order-placement concerns behind the interface a PPO policy is supposed to
replace cleanly.

## Rung table

For the active zone, `rung_spacing_pct` (a fraction of price) determines
rung density rather than a fixed count, so wider zones automatically get
more rungs and narrow zones fewer:

```
rung_count   = ceil(zone_span / (rung_spacing_pct * zone.resistance))
rung_prices  = rung_count + 1 points evenly spaced between support and resistance
```

Each rung's *cumulative* target reuses the existing pure functions from
`strategy.py` unchanged — `distance()` and `target_notional()` — evaluated
at that rung's own price instead of the current mark price:

```
d_rung      = strategy.distance(rung_price, zone, trend)
target_rung = strategy.signed(strategy.target_notional(d_rung, max_n, alpha), trend)
```

A rung's actual order size is the delta between its cumulative target and
its neighbor's — the same "order only the difference" principle
`strategy.decide()` already uses, just sampled at fixed price points
instead of continuously. This is why the sell-side rungs (near the
unfavourable edge, low `d`) come out smaller than the buy-side rungs near
the favourable edge: it falls directly out of the existing `alpha` curve,
not a separate rule.

**Side is relative to current mark price, not zone position**: a rung
priced below current mark price rests as a BUY, above it as a SELL
(mirrored for `trend=short`). This is what makes the ladder self-managing
as price moves back and forth — a rung's role flips between accumulate and
trim depending only on where price currently sits, without needing the
rung's price or cumulative target recomputed.

## Liquidation-aware buy-side cap

Repeated whipsaws across the same rungs would otherwise let the position
compound past what `max_notional` intends, because refilling a rung after
its counterpart sell rung hasn't caught up yet is still a real fill. The
brake:

1. **Survival target price** — the price the position must survive to
   without liquidating. Zones are contiguous (`zones[i].support ==
   zones[i+1].resistance`), so for every zone except the ladder's lowest
   (`trend=long`; highest for `short`):

   ```
   survival_price = next_zone.resistance - liquidation_buffer_pct * next_zone_span
   # == current_zone.support - liquidation_buffer_pct * next_zone_span
   ```

   At the ladder's edge zone, where there is no next-lower zone, the same
   formula is applied with the edge zone's own span in place of
   `next_zone_span`:

   ```
   survival_price = edge_zone.support - liquidation_buffer_pct * edge_zone_span
   ```

   (Both mirrored — using `resistance` instead of `support`, and the zone
   *above* instead of below — for `trend=short`.)
2. **Projected max additional quantity** — `leverage_brackets` gives the
   maintenance-margin tier for the current/projected notional bracket.
   `ladder.py` algebraically inverts Binance's isolated-margin liquidation
   formula for that tier to solve directly for the maximum additional
   quantity the buy side may accumulate before liquidation price would
   reach the survival target.
3. If the rung table's natural buy-side total exceeds that cap, the
   remaining **unfilled** buy rungs are scaled down proportionally so the
   capped total holds. Already-filled rungs and the sell side are
   untouched.
4. **Backstop:** Binance's actual reported `liquidationPrice` (already
   available from the position-risk payload `get_snapshot` fetches) is
   checked every tick regardless of the projection. If it has already
   crossed the survival target, every remaining buy rung is cancelled
   immediately — the projection sizes rungs *in advance*, the reported
   figure is the ground truth that can override it.

## Lifecycle

**Zone activation** (first entry into a zone, or after a `STOP_OUT`/
`HALT_FLATTEN` market-flattened the previous position): the position is
guaranteed flat, so the rung table is computed fresh against current price
and zero position, capped, and every rung placed.

**Each tick while a zone's ladder is live:**

1. Fetch open orders for the symbol (new REST call — see Rate limits
   below) and diff against the tracked rung→order-id map to detect fills.
2. No fill since last tick → nothing to do; the ladder rests as-is.
3. A fill detected → enter **settling**: record the current open-order
   snapshot rather than reconciling immediately.
4. While settling: if the open-order set is unchanged from the previous
   tick (no *new* fill since settling began), the market is considered
   stable. Recompute the rung table against the current actual position
   and current mark price, apply the liquidation cap, and **validate** it
   — every planned BUY rung must price below current mark price and every
   SELL rung above it, and every non-zero rung must clear `min_qty`/
   `min_notional`.
   - Validation fails (price moved during the stability check) → stay in
     settling, place nothing, retry next tick.
   - Validation passes → reconcile: cancel orders for rungs whose price/
     side/quantity changed, place orders for newly-needed rungs, leave
     unchanged rungs alone. Clear settling.
5. If settling and a *new* fill arrives before stabilizing → update the
   snapshot and keep waiting; reconciliation never fires against a moving
   target.

**Zone change or HALT:** cancel every resting ladder order for the zone
being left, *then* the existing market-order flatten fires — unchanged
from today except for the added cancellation step.

## Margin mode

`market.set_margin_type(client, symbol, "ISOLATED")` is called at startup
and on symbol switch, following the same pattern as `set_leverage`.
Isolated margin makes `liquidationPrice` a function of that position's own
allocated margin only, which is what makes the liquidation-cap projection
in the previous section tractable — in CROSSED mode the same position's
liquidation price depends on the whole wallet's other exposure.

Binance rejects the margin-type change in two cases:

- **Already isolated** (`-4046`) — treated as a no-op success.
- **An open position exists in CROSSED mode** — logged as a warning and
  the bot continues running in whatever mode is already active, rather
  than refusing to start. This is a risk-profile setting, not a
  correctness invariant like the existing wrong-side/oversized-position
  startup guards, but the warning matters: the liquidation-cap projection
  in the previous section is wrong until the switch actually takes
  effect, so a shrunk-but-still-CROSSED ladder should be read with that
  caveat until the position clears and the mode switch can retry.

## Rate limits

`get_open_orders` adds one REST call to every tick a ladder is live,
beyond the three `get_snapshot` already makes. This is a deliberate
tradeoff the existing three-call budget (11 weight/tick, chosen to stay
well under Binance's 2400/min limit) does not currently make room for, so
the implementation plan must re-total the weight budget at the configured
`poll_seconds` before this ships, the same way the existing design
document sized the original three-call budget.

`leverage_brackets` does not need to be fetched every tick — the
maintenance-margin tier table for a symbol changes rarely. It is fetched
once at startup and on symbol switch, cached, and only re-fetched if a
projected notional would fall outside the cached tier boundaries.

## Journal and notifications

Order-journal records for ladder fills carry the existing order-record
schema, plus:

```
rung_index, rung_price, liquidation_capped (bool)
```

Tick-journal records gain, while a ladder is live:

```
ladder_rungs_total, ladder_rungs_filled,
liquidation_price, liquidation_survival_target
```

`notify.py` gains one new event, `liquidation_brake`, pushed when the
buy-side cap actually shrinks remaining rungs or when the reported
`liquidationPrice` backstop cancels them outright — the same class of
risk event `HALT` already covers, and for the same reason: an operator
who has walked away needs to know the bot intervened to avoid liquidation
even though nothing failed outright.

## Testing

- `ladder.py`: pure unit tests in the style of `test_strategy.py`/
  `test_execution.py` — rung-table generation against hand-computed
  values, buy/sell split at various prices, the liquidation-cap algebra
  against manually derived numbers for a known maintenance-margin tier,
  validation rejecting mis-ordered rungs, and the diff/reconciliation
  planner.
- `market.py`: extractor tests with fixture payloads for `liquidationPrice`
  and `leverage_brackets`, and `set_margin_type` against a `FakeClient`
  (including the `-4046` no-op case and the open-position warning case).
- `execution.py`: `place_limit_order`/`cancel_orders` tests via
  `FakeClient`, mirroring the existing `test_execute_*` pattern.
- `bot.py` integration: extend `test_tick_throttle.py`'s `LoopClient` with
  `get_open_orders`/`cancel_order`/limit-order support to drive a full
  ladder lifecycle (placement → simulated fill → settling → reconciliation)
  end-to-end through `bot.run()`, the same way the existing suite drives
  the market-order loop today.

## Risk notes

- **Poll-based fill detection is up to one `poll_seconds` late.** A
  websocket user data stream would detect fills in real time, but
  introduces a persistent connection, listen-key keepalive, and
  reconnect/backoff logic — a meaningfully larger architectural surface
  than this codebase currently carries anywhere (see `notify.py`'s
  deliberate avoidance of threads/queues). Deferred; see Future
  Extensions.
- **The liquidation-cap projection is a model, not a guarantee** — it
  depends on the `leverage_brackets` tier data being current and the
  isolated-margin formula being applied correctly. The reported
  `liquidationPrice` backstop exists specifically because the projection
  can be wrong; it is not a substitute for the backstop.
- **Settling adds latency to reconciliation during fast, one-directional
  moves** — a rung that should flip sides may sit unreconciled for
  multiple ticks if fills keep arriving. This is intentional (the
  alternative is reconciling against a still-moving target and
  mis-placing orders), but it means the ladder can temporarily lag the
  ideal curve more than the old continuous market-order model ever did.
- **`get_open_orders` every tick is a new, ongoing rate-limit cost** for
  as long as a ladder is live, not just at order-sending ticks the way
  the market-order model's cost was.

## Future extensions

- **Websocket user data stream fill detection.** Real-time order-update
  events instead of poll-based diffing, once the added architectural
  surface (persistent connection, listen-key keepalive, reconnect
  handling) is worth taking on.
