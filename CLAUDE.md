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
- `futures/settings.py` — `Settings`/`Zone` dataclasses and a validating `load()`. Two fields exist
  only for the ladder: `rung_spacing_pct` (rung spacing as a fraction of resistance, see
  `ladder.rung_prices`) and `liquidation_buffer_pct` (the safety margin `ladder.survival_price` applies
  beyond the next zone). Both are hot-reloaded like every other field, so both are validated the same
  way (`(0, 1)` / `[0, 1)`) rather than trusted as startup-only constants.
- `futures/settings.json` — runtime config (symbol, trend, leverage, alpha, exposure fraction,
  rebalance threshold, stop buffer, poll interval, testnet flag, rung spacing, liquidation buffer, zone
  ladder). Re-read every tick — see architecture notes below.
- `futures/client.py` — `build(testnet)` constructs the Binance `UMFutures` client; also measures the
  offset between this machine's clock and Binance's server clock at startup and patches
  `binance.api.get_timestamp` with it.
- `futures/market.py` — exchange reads: `get_snapshot()` returns a frozen `Snapshot` (mark price,
  signed position amount, unrealized PnL, wallet balance, available balance, entry price, isolated
  wallet, liquidation price) from one call per endpoint, built out of pure extractors
  (`wallet_balance_from`, `available_balance_from`, `position_amt_from`, `unrealized_pnl_from`,
  `entry_price_from`, `isolated_wallet_from`, `liquidation_price_from`) that take an already-fetched
  payload — the three ladder-only fields ride along on the same position-risk payload `position_amt`
  already reads, so they cost no extra call and share its instant exactly. Also `get_position_amt()`
  for a one-symbol read outside the tick snapshot, `get_filters()` (lot step, min qty, min notional,
  tick size), `get_open_orders()` (polled every tick a ladder is live, to detect fills by diffing
  against tracked rung ids), `get_leverage_brackets()` (the maintenance-margin bracket table, fetched
  once at startup/symbol-switch and turned into a `MarginTier` by `maintenance_tier_from()`),
  `set_leverage()`, and `set_margin_type()` (best-effort switch to ISOLATED margin, tolerating both
  "already set" and "position already open" as non-fatal).
- `futures/strategy.py` — pure decision logic. `decide()` takes plain values and returns a `Decision`;
  see architecture notes for why this module has no I/O.
- `futures/ladder.py` — pure limit-order-ladder computation, the counterpart to `strategy.py` for
  in-zone `SCALE_IN`/`SCALE_OUT`: it turns `strategy.py`'s continuous target curve into a discrete set
  of resting rung orders (`rung_table`, `build_rung_orders`, `desired_orders`), the liquidation-aware
  cap on the accumulate side (`liquidation_scale`, `survival_price`, `apply_liquidation_cap`), and the
  order-book diff that minimizes churn on reconciliation (`plan_orders`). No I/O, no client, no clock,
  for the same reason `strategy.py` has none — see architecture notes and
  `docs/superpowers/specs/2026-09-17-limit-order-ladder-design.md` for the full derivation.
- `futures/execution.py` — quantity flooring to the lot step, exchange filter checks, market- and
  limit-order placement (`place_limit_order`), side-aware price rounding to the tick size (`price_for`),
  and order cancellation/lookup (`cancel_orders`, `cancel_all_orders`, `query_order_result`).
- `futures/journal.py` — two JSONL logs under `futures/logs/` (gitignored): `ticks-YYYY-MM-DD.jsonl`
  (one line per record the loop hands it — a repeated state is written at most once per interval, see
  architecture notes below; `journal.py` writes, it does not decide) and `orders-YYYY-MM-DD.jsonl` (one
  line per executed order, carrying a full environment snapshot at fill time).
- `futures/notify.py` — Telegram push notifications: `enabled()`, `send()`, one pure formatter per
  event (`format_order`, `format_halt`, `format_startup_refused`, `format_config_refused`,
  `format_liquidation_brake`) and the event entry points `bot.py` calls (`order_executed`, `halt`,
  `startup_refused`, `config_refused`, `liquidation_brake`). Formatting and sending only — no trading
  logic, no decisions. Every entry point returns a bool and swallows `Exception`, so a notification
  failure can never reach the loop; see architecture notes.
- `futures/bot.py` — the polling loop (entry point). Owns the loop's mutable state across iterations:
  `active_index` (which zone is currently being worked), `halted` (whether the HALT-left-the-ladder
  notice has already been announced, and re-armed once price returns to the ladder), `refused_cfg` (the
  settings edit whose reload was last refused), a `LadderState` instance (the resting-order lifecycle
  for the currently active zone — see architecture notes), and one `TickLog` per journal record stream
  — `tick_log`, `config_log`, `error_log`.

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

It may also define two optional names:
- `telegram_token`, `telegram_chat_id` — the bot token and chat id `notify.py` posts to. Both missing
  or empty means notifications are simply off; that is a supported state, not an error, and it must
  stay silent rather than warn.

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

Tests live under `tests/`, run with:

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
- **Sizing uses TOTAL wallet balance (`Snapshot.wallet_balance`, from `market.wallet_balance_from`,
  which reads the payload's `balance` field), never available balance (`Snapshot.available_balance`,
  from `market.available_balance_from`, which reads `availableBalance`).** Available balance shrinks as
  margin is consumed by an open position, which would shrink the computed target notional as the
  position fills and stall accumulation short of its intended size. `available_balance` exists and is
  logged for visibility, but must not feed `max_notional`.
- **One REST call per endpoint per tick** (`market.get_snapshot`: `ticker_price`,
  `get_position_risk`, `balance` — 3 calls, 11 request weight). Every value the tick needs is derived
  from those three payloads by the pure extractors; do not add a second fetch of an endpoint the
  snapshot already read. **The snapshot is no longer the whole tick, though.** Current per-tick
  budget, re-totalled against the limit-order ladder: **~12 weight in steady state** — 11 for the
  snapshot plus 1 for `_detect_fill`'s `get_open_orders`, polled on every tick a ladder is resting
  (there is no fill event to subscribe to). A tick that finds a fill costs **~13 + N** (the detect
  call, one `query_order` per fill being looked up, and the settling re-snapshot); a settling tick
  that reconciles is the same shape, its `query_order`s being the pruned rungs. At `poll_seconds: 1`
  that is roughly **720–840 weight/min against Binance's 2400/min limit — safe, with comfortable
  headroom.** Re-total this bullet whenever an endpoint is added to the tick: it is what the next
  change will budget against. Headroom is not a spare-capacity argument — the earlier five-call tick
  cost 21 weight, 1260/min, and being rate limited means an IP ban while holding a leveraged
  position the bot then cannot flatten. It also narrows the tick from five instants to three - the two
  values taken from the positions payload agree with each other, as do the two from the balances
  payload - but three sequential round-trips are still three moments, so `Snapshot` is **not** an
  atomic view and nothing should assume its fields are mutually consistent.
- **The tick journal throttles repeated state, not "uneventful actions"** (`bot.TickLog`, floor
  `bot.TICK_LOG_INTERVAL_SECONDS`). A tick record is written when an order is being sent, when the
  tick's `(action, reason)` differs from the last record written, or when the interval has elapsed
  since that record — otherwise it is skipped. Do **not** throttle on the action label instead: several
  states are sticky, not momentary (`HALT` repeats every tick once price leaves the ladder, a residual
  position below `min_notional` repeats `SCALE_OUT` that `is_executable` always rejects, an invalid
  `settings.json` repeats `config_error`), so "always write anything that is not `HOLD`/`IDLE`" floods
  the log in exactly the walked-away-operator case the throttle exists for. Transitions are never lost:
  the first `HALT` tick and the tick that comes back out of it both differ from the record before them.
  The interval is elapsed `time.monotonic()`, not a tick count, so the rate survives a change to
  `poll_seconds`. **One `TickLog` per record stream, never a shared one**: the tick, `config_error`,
  and `error` streams interleave within a tick, so through a single log every record would look like a
  change from the last and none would throttle. `config_error` is keyed on its message and the loop
  error handler's `error` record on `("error", <message>)` — a persistently rejected order (`-2019`
  when required margin exceeds the wallet, which is where `exposure_fraction: 1.0` puts the strategy at
  its largest) would otherwise write a line every poll, while a NEW distinct failure is still recorded
  the instant it happens. `startup_refused` and `config_reloaded` call `journal.log_tick` directly and
  are never throttled; `config_refused` is deduped separately by `refused_cfg`. A malformed
  `settings.json` must be caught as a `config_error` — the reload handler catches `TypeError` as well
  as `ValueError`/`KeyError`/`OSError`, because `"leverage": null` reaches `int(None)` and a
  `TypeError` escaping there becomes an unthrottled loop error every poll. Never turn this into
  "write one tick in sixty" — the tick
  journal is the audit trail and the dataset a PPO policy will train on, so blanket sampling would
  discard precisely the interesting events. Order-journal behaviour is unconditional: every executed
  order writes a record.
- **A successful settings reload writes a `config_reloaded` record** (`bot._log_config_reloaded`,
  carrying the changed fields as `{field: [old, new]}`). Under the throttle the next tick record can be
  up to a minute away, so without this the journal cannot say when an edit actually took effect. It is
  the counterpart to `config_refused`; keep both.
- **Telegram notifications may never affect trading** (`futures/notify.py`). Six events are pushed:
  every executed order (never throttled — fills are rare and each moves real money, and this covers both
  a market-order fill and an individual ladder rung's fill — see `_journal_ladder_fill`), the transition
  into `HALT`, `startup_refused`, `config_refused`, every loop `error` record the throttle lets through,
  and the liquidation-cap brake engaging (`liquidation_brake`, pushed whenever the accumulate side is
  shrunk or cancelled outright — see the ladder lifecycle notes below). Three rules hold this together. (1) *It cannot
  raise into the loop*: `send()` and every event entry point swallow `Exception` and return a bool, and
  the failure path is itself guarded and forced to ASCII — an f-string of a localised `OSError` on a
  `cp949` console is how the bot was killed once before. (2) *It cannot stall the loop*: every request
  carries an explicit `notify.TIMEOUT_SECONDS` timeout; there is deliberately no thread and no queue.
  (3) *It disappears when unconfigured*: credentials are read with `getattr(config, ..., None)`, and
  absent or empty means silently off — no warning, no log line. Notifications are sent **after** the
  corresponding journal write and formatted from the same record, so a Telegram problem can never cost
  an order-journal line and the message can never disagree with the audit trail. **The HALT
  notification is sent after the flatten has been attempted, never before it** — the flatten is the
  response to an adverse move and must not wait on an HTTP call — and it reports the outcome
  (`notify.FLATTENED` / `FLATTEN_FAILED` / `NOT_FLATTENED`); the failed-flatten case is notified from
  the `except` around `execution.execute` and before the re-raise, because by the next tick `halted`
  has latched and nothing would ever say a HALT happened. Sticky states notify on the transition,
  reusing the loop state that already exists for that (`halted` for `HALT`, the
  `announce`/`refused_cfg` dedupe for `config_refused`, and the *return value* of `error_log.log` for
  loop errors) rather than adding a second throttle — note `TickLog` is not reusable here, it writes
  to `journal.log_tick` itself. Exchange/OS text that goes into a message passes through
  `notify._safe_text` (redacted, forced to ASCII, clipped). The whole test suite is barred from
  sending by an autouse fixture in `tests/conftest.py`; keep it that way when adding tests that drive
  `bot.run()`.
- **Order quantities floor to the lot step, never round up** (`execution.quantity_for` uses
  `math.floor`). Rounding up would let the bot exceed its own exposure cap on the last partial step of a
  fill — flooring is the only direction that cannot overshoot.
- **In-zone scaling rests as limit orders; every exit stays a market order** (`bot.run`'s
  `in_zone_ladder_case`). The split is on `decision.reason`, not `decision.zone_index`: only
  `SCALE_IN`/`SCALE_OUT` decided *inside* the active zone route through `ladder.py`/`_place_ladder`/
  `_reconcile_ladder` and `execution.place_limit_order` — a rung ladder resting on the book instead of
  crossing the spread every tick (`in_zone_ladder_case = decision.zone_index is not None and
  decision.reason in (SCALE_IN, SCALE_OUT)`). `HALT_FLATTEN` and the dead-band `SCALE_OUT` do have
  `zone_index is None` (there is no active zone to scale within), but `STOP_OUT` does NOT — `strategy.
  decide()` assigns `STOP_OUT` precisely when the newly-selected zone differs from `active_index` while a
  position is held, so its `zone_index` is the new zone, not `None`; what excludes it from the ladder
  path is that its `reason` is `STOP_OUT`, not `SCALE_IN`/`SCALE_OUT`. All three exits are unchanged from
  before the ladder existed: they still go through `execution.execute` (market). The split is deliberate
  — an exit is the tick the bot decided the position must change size *now*, where certainty of execution
  matters more than price, while in-zone scaling has no such urgency and can afford to wait at its own
  limit price. Never route an exit through the ladder (it can't guarantee a fill before the next tick),
  and never give in-zone scaling a market order "for speed" — that reintroduces the exact spread cost the
  ladder exists to save.
- **The ladder's lifecycle is poll-detect-settle-reconcile, not fill-driven** (`bot.py`'s `LadderState`,
  `_place_ladder`, `_detect_fill`, `_reconcile_ladder`). There is no fill event to subscribe to:
  `_detect_fill` diffs the tracked rung-price -> order-id map against `market.get_open_orders` every
  tick a ladder is live, so a rung leaving the book is discovered up to one `poll_seconds` late, and
  looks identical whether it filled or was cancelled out from under the bot (only `query_order_result`
  tells the two apart, in `_journal_ladder_fill`). A detected departure does not trigger an immediate
  rebuild — reconciling mid-cascade would price a whole new ladder off a position still in the middle of
  filling, once per poll for as long as the move lasts. Instead the detecting tick only journals the
  departed rung (`_log_ladder_fills`) and arms `ladder_state.settling` with a snapshot of the open-order
  id set; later ticks re-snapshot for as long as that set keeps changing, and only a tick that finds it
  *unchanged* since the wait began runs `_reconcile_ladder`, which rebuilds the ladder as a minimal diff
  (`ladder.plan_orders`) against the position the fills actually left behind, not a blind cancel/replace.
  `_place_ladder` is the one step with no fill history to reconcile against, so it only runs on zone
  activation — first entry into a zone, or the tick after a zone change or a market-order flatten emptied
  the tracked set — never to patch a partially-filled ladder back up.
- **The liquidation cap is a projection with a ground-truth override, not just a projection**
  (`ladder.liquidation_scale`/`survival_price` vs. `bot._liquidation_breached`). Every placement or
  reconcile computes a safety factor `scale` in `[0, 1]` from Binance's own isolated-margin liquidation
  formula, projecting where the liquidation price would land if every remaining accumulate-side rung
  filled at its planned size, and shrinks only that side (never the trim side, which already reduces
  risk) accordingly. That projection answers "would this accumulation, once filled, be safe" — which can
  read "safe" for a position that is *already* unsafe right now, because the projected fills improve the
  average entry over time. `_liquidation_breached` exists for exactly that gap: whenever Binance's own
  reported `liquidationPrice` (`Snapshot.liquidation_price`) has already crossed `survival_price`, it
  forces `scale = 0.0` regardless of what the projection would otherwise say — ground truth about where
  the position stands now beats a projection about where it would end up. The same predicate also drives
  a third, independent guard: the backstop near the end of the in-zone tick body in `run()`, which runs on
  *every* in-zone tick no matter which branch above it took (placement, settling, or an ordinary resting
  ladder) and, the instant the reported price crosses, pulls the accumulate side off the book outright
  with a per-id `execution.cancel_orders` (never the whole-symbol sweep `_cancel_ladder` uses) rather than
  waiting for the next placement or reconcile to notice. See the design doc's "Liquidation-aware buy-side
  cap" for the formula; do not remove `_liquidation_breached` or trust the projection alone, since a
  position that already reads breached is precisely the case it was added to catch.
- **The trim side has its own cap, separate from the liquidation cap and against a different fact**
  (`ladder.trim_scale`/`apply_trim_cap`, applied in both `_place_ladder` and `_reconcile_ladder`
  alongside the existing accumulate-side liquidation cap). `desired_orders()` prices the trim side
  (SELL for `long`, BUY for `short`) purely from the zone's geometry and the current price — it has no
  idea how much of a position it is trimming actually exists. Uncapped, a zone activation from flat (or
  from a position the accumulate side has not caught up with yet) rests trim rungs sized as if the
  position already sat at this zone's full target. If price ran through one of them before the
  accumulate side had filled anything, the fill would go through against a smaller position than the
  rung assumed — at the extreme, a completely flat one — and the exchange (no hedge mode) would open a
  position in the OPPOSITE direction from the configured trend: a SHORT out of a `long`-trend ladder, or
  a LONG out of a `short`-trend one. `trim_scale` closes this the same way `liquidation_scale` closes
  the accumulate side's own risk: a factor `k` in `[0, 1]`, computed by converting every trim rung's
  notional through ITS OWN price into quantity and comparing the total to the position that is REALLY
  open (read off the same snapshot the accumulate cap uses), then `apply_trim_cap` scales every trim
  rung by it uniformly. `k = 0.0` at a flat position — no trim rung goes out at all until an accumulate
  fill gives the ladder something to trim against — and `k` rises with the position on every later
  placement and reconcile too, not just once at activation, so a growing position progressively unlocks
  more of the trim side as it goes. This is deliberately a separate cap from the liquidation guard, not
  folded into it: the liquidation cap answers "is this accumulation safe" and only ever touches the
  accumulate side; `trim_scale` answers "does this position actually exist" and only ever touches the
  trim side, and the two must stay independent because a position can be safe from a liquidation
  standpoint while still being too small to back the trim side's full geometry (the common case, on
  every fresh activation).
- **A fill that first becomes visible on the same poll as an exit is not journalled — a known,
  deliberately deferred gap** (`bot._log_ladder_fills`'s docstring). Every exit — `STOP_OUT`,
  `HALT_FLATTEN`, the dead-band `SCALE_OUT`, a zone change, a symbol change — tears the ladder down
  through `_cancel_ladder`, one bulk `execution.cancel_all_orders` call with no per-id status query
  first, because the flatten behind it must not wait on a round-trip per rung. If a rung filled since the
  last poll on the very tick that also decides an exit, the fill is never detected — the exit branch runs
  first and the tick never reaches `_detect_fill`/`_reconcile_ladder` — and `_cancel_ladder` simply
  discards the tracked id, so nothing ever asks the exchange what became of it. This is intentional, not
  an oversight to quietly patch: closing it would mean snapshotting the tracked ids before every teardown
  and querying them after the flatten has gone out, across every exit call site, and no exit call site
  may grow a pre-order round-trip to do it. Do not add a "quick" fix that queries fills before an exit —
  that reintroduces the exact latency the exit path exists to avoid.
- **`run()` sweeps the symbol once at startup, before the loop** (`execution.cancel_all_orders`, right
  after the startup reconciliation guard). The rung→id map lives in memory and dies with the process,
  so after any restart — deploy, crash, reboot — the previous run's rungs are still resting and this
  one knows nothing about them. `_cancel_ladder` cannot clean them up: it returns early when nothing is
  tracked, which on a fresh start is always (keep that guard — it is what holds the sticky exit states
  to one request in total). Without the sweep the first in-zone tick places a *second* complete ladder
  beside the first: up to double the configured exposure, with the liquidation cap projecting against
  only the half it placed and `_detect_fill` blind to the orphans' fills, which then move the position
  with no order-journal line at all. It is deliberately unguarded — a failure ends startup loudly,
  before any order exists, which is the recoverable outcome; double exposure on a leveraged account is
  not. It runs *after* the refusal guard: a position the bot refuses to adopt is somebody else's trade,
  and so are the orders working it.
- **`settings.json` is re-read every tick** (`bot.py`'s loop calls `settings.load()` each iteration).
  This lets trend, leverage, and the zone ladder change without restarting the bot. An invalid edit
  (caught as `ValueError`/`KeyError`/`OSError`) is logged to the tick journal as a `config_error` and
  otherwise ignored — the prior valid `Settings` stay in force. Never make a settings-load failure
  fatal to the loop. **An applied reload that changes `zones`, `trend`, `alpha`, `exposure_fraction`,
  `rung_spacing_pct` or `leverage` tears the ladder down first** (`_cancel_ladder`, against the OLD
  `cfg` — that is where the rungs are resting — and before `set_leverage`, which the exchange can
  refuse while orders are open). Nothing else would re-price them: placement is gated on the zone index
  changing or the tracked set being empty, reconciliation is armed only by a detected fill, and in-zone
  scaling no longer market-orders, so without this a trend flip takes effect on no tick at all and the
  old direction's accumulate rungs keep building the position the operator just abandoned. The empty
  tracked set alone re-arms placement, so the next tick rebuilds from scratch.
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
- `bot.py` holds the loop's mutable state — `active_index`, `halted`, `refused_cfg`, `ladder_state`, and
  the `tick_log`/`config_log`/`error_log` `TickLog` instances; `market.py`, `strategy.py`,
  `execution.py`, `ladder.py`, `journal.py`, and `notify.py` are stateless and take all inputs as
  arguments — don't reintroduce module-level mutable state into them.
