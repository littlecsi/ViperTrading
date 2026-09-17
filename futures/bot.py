import time
import traceback
from dataclasses import asdict, fields, is_dataclass

import client
import execution
import journal
import market
import notify
import settings
import strategy

# A tick that repeats the state of the one before it is journalled at most this
# often. At a one-second poll the unthrottled log wrote ~86400 lines (~35 MB) a
# day, nearly all of them identical.
TICK_LOG_INTERVAL_SECONDS = 60.0


class TickLog:
    """The tick journal's write policy: what got written, and when.

    Throttling on the action LABEL (write anything that is not HOLD/IDLE) does
    not work, because several of the loop's states are sticky rather than
    momentary. Once price leaves the zone ladder every tick decides HALT; a
    residual position too small to close decides SCALE_OUT forever while
    is_executable rejects it every time; a settings.json left invalid raises on
    every reload. Each of those writes one line per poll for as long as it
    lasts - which is exactly the walked-away-operator scenario the throttle
    exists for.

    So the question is not what the action is called but whether this tick did
    anything or changed anything:

      - an order is being sent, or
      - the (action, reason) pair differs from the last record written, or
      - TICK_LOG_INTERVAL_SECONDS have passed since the last record.

    A transition is therefore always on the record - the tick that first
    decides HALT is written the moment it happens, as is the tick that comes
    back out of it - while a steady repeat of one state collapses to a line a
    minute. Nothing is ever sampled one-in-N: the tick journal is the audit
    trail and the state/action history a PPO policy trains against, and blanket
    sampling would thin out precisely the events it exists to capture.

    Records that are discrete by nature bypass this and call journal.log_tick
    directly: startup_refused (it fires once, and losing it would hurt),
    config_reloaded (a reload happens once), and config_refused (already
    deduped by the loop's own refused_cfg, which is why the two mechanisms
    never see each other).

    Each record stream needs its OWN instance. The streams interleave within a
    tick, so one shared log would see alternating keys, call every record a
    change from the last, and throttle none of them."""

    def __init__(self, interval: float = TICK_LOG_INTERVAL_SECONDS, clock=time.monotonic):
        self._interval = interval
        self._clock = clock
        self._last_key = None
        self._last_at = None

    def should_write(self, key, forced: bool, now: float) -> bool:
        if forced or self._last_at is None or key != self._last_key:
            return True
        return (now - self._last_at) >= self._interval

    def log(self, record: dict, key, forced: bool = False) -> bool:
        """Journal `record` unless it is a throttled repeat. Returns whether it
        was written.

        `key` identifies the state this record describes - equal keys are the
        same state repeating. Elapsed clock time, not a tick count, so the
        floor stays one-a-minute whatever poll_seconds is set to."""
        now = self._clock()
        if not self.should_write(key, forced, now):
            return False
        # Marked only once the record is actually on disk. Marking first would
        # let a failed write (the journal file is opened every tick, and a
        # Windows file lock on it is the very hazard the loop's error handler
        # is built around) silence this state for the rest of the interval.
        journal.log_tick(record)
        self._last_key = key
        self._last_at = now
        return True


def _config_changes(old, new) -> dict:
    """Which settings fields differ, as {field: [old, new]}, JSON-safe."""

    def plain(value):
        if isinstance(value, tuple):
            return [plain(v) for v in value]
        if is_dataclass(value):
            return asdict(value)
        return value

    return {
        f.name: [plain(getattr(old, f.name)), plain(getattr(new, f.name))]
        for f in fields(new)
        if getattr(old, f.name) != getattr(new, f.name)
    }


def _log_config_reloaded(cfg, changes: dict) -> None:
    """Record that an edited settings.json actually took effect.

    A successful reload used to be visible only as a console line, which was
    survivable while the next tick's record - carrying the new trend, leverage
    and zone bounds - was at most a second behind it. Under the throttle that
    record can be a minute late, so without this the journal cannot say when a
    config took effect. Counterpart to config_refused, and exempt from the
    throttle: a reload is a discrete event, never a sustained state."""
    journal.log_tick(
        {
            "action": "config_reloaded",
            "symbol": cfg.symbol,
            "trend": cfg.trend,
            "leverage": cfg.leverage,
            "changed": changes,
        }
    )


def _ascii(value) -> str:
    """Console-safe text.

    This machine's console codepage is not UTF-8, and exchange/OS error
    messages can arrive localised. A print() that raises UnicodeEncodeError
    inside the error handler would end the process while it holds a leveraged
    position, so every dynamic string printed from the handler goes through
    here first."""
    return str(value).encode("ascii", "replace").decode("ascii")


def _sleep(seconds) -> None:
    """Sleep the poll interval, clamped, and unable to raise.

    settings.load() already rejects poll_seconds < 1, but this call sits
    outside the per-tick try/except: anything that raises here ends an
    unattended bot. Clamping locally means no config value can reach
    time.sleep() unguarded even if it arrives by some other route."""
    try:
        delay = max(1.0, float(seconds))
    except (TypeError, ValueError):
        delay = 1.0
    time.sleep(delay)


def run() -> None:
    cfg = settings.load()
    api = client.build(cfg.testnet)

    market.set_leverage(api, cfg.symbol, cfg.leverage)
    filters = market.get_filters(api, cfg.symbol)

    label = "TESTNET" if cfg.testnet else "LIVE"
    print(f"Viper starting on {label} - {cfg.symbol} @ {cfg.leverage}x, trend={cfg.trend}")

    # Startup reconciliation guard. The sizing logic assumes it owns the whole
    # position on the configured symbol, so a pre-existing position that is
    # larger than this bot would ever take, or is on the wrong side of the
    # configured trend, is somebody else's trade. Reconciling it would happen
    # silently in one market order on the first tick.
    start = market.get_snapshot(api, cfg.symbol)
    start_amt = start.position_amt
    start_notional = start_amt * start.mark_price
    start_max_n = strategy.max_notional(
        start.wallet_balance, cfg.leverage, cfg.exposure_fraction
    )
    wrong_side = (cfg.trend == settings.LONG and start_notional < 0) or (
        cfg.trend == settings.SHORT and start_notional > 0
    )
    if wrong_side or abs(start_notional) > start_max_n:
        problem = "opposes trend" if wrong_side else "exceeds the exposure cap"
        print(f"REFUSING TO START: existing {cfg.symbol} position {problem}.")
        print(f"  position: {start_amt} ({start_notional:.2f} USDT at {start.mark_price})")
        print(f"  trend: {cfg.trend}, max notional this bot would hold: {start_max_n:.2f} USDT")
        print("  Flatten or reconcile that position manually, then start the bot again.")
        refusal = {
            "action": "startup_refused",
            "reason": "unreconciled_position",
            "symbol": cfg.symbol,
            "testnet": cfg.testnet,
            "position_amt": start_amt,
            "current_notional": start_notional,
            "max_notional": start_max_n,
            "trend": cfg.trend,
            "wrong_side": wrong_side,
        }
        journal.log_tick(refusal)
        # Notified from the same dict, and after the journal write: a Telegram
        # problem must never cost the record. notify cannot raise.
        notify.startup_refused(refusal)
        return

    active_index = None
    halted = False
    # One write policy per record stream. They must not share: the streams
    # interleave within a tick, so through a single log each record would look
    # like a change from the last one written and none of them would throttle.
    tick_log = TickLog()
    config_log = TickLog()
    error_log = TickLog()
    # The config whose reload was last refused. Refusals are re-evaluated every
    # tick (the operator may flatten, or edit the file again), but only
    # announced when the refused config changes, so a walked-away operator does
    # not come back to 17k identical lines.
    refused_cfg = None

    while True:
        try:
            # Re-read settings each tick so trend, leverage, and zones can be
            # changed without a restart. Invalid edits are ignored, not fatal.
            # An applied reload is journalled AFTER this block, not inside it:
            # a journal write that failed here would be recorded by the handler
            # below as a config_error, which is the opposite of what happened.
            reloaded = None
            try:
                new_cfg = settings.load()
                if new_cfg != cfg:
                    announce = new_cfg != refused_cfg
                    if new_cfg.testnet != cfg.testnet:
                        # Nothing downstream re-reads cfg.testnet, and rebuilding
                        # the client here would swap accounts underneath an open
                        # position. Refuse the WHOLE reload: adopting the rest of
                        # the file while silently ignoring a live/testnet switch
                        # is how an operator "makes it safe" and walks away from a
                        # bot still trading real money.
                        refused_cfg = new_cfg
                        if announce:
                            print("*** SETTINGS RELOAD REFUSED ***")
                            print(
                                f"  'testnet' changed {cfg.testnet} -> {new_cfg.testnet}; "
                                "switching between live and testnet requires a RESTART."
                            )
                            print(
                                f"  The bot is STILL RUNNING on {label} with the previous "
                                "settings. No part of the new file was applied."
                            )
                            refusal = {
                                "action": "config_refused",
                                "reason": "testnet_change_requires_restart",
                                "current_testnet": cfg.testnet,
                                "rejected_testnet": new_cfg.testnet,
                            }
                            journal.log_tick(refusal)
                            notify.config_refused(refusal)
                    elif new_cfg.symbol != cfg.symbol:
                        # Adopting a new symbol while the old one holds a position
                        # orphans that position: nothing would stop it out, scale
                        # it out, or flatten it on a halt.
                        old_amt = market.get_position_amt(api, cfg.symbol)
                        if old_amt != 0:
                            refused_cfg = new_cfg
                            if announce:
                                print("*** SETTINGS RELOAD REFUSED ***")
                                print(
                                    f"  'symbol' changed {cfg.symbol} -> {new_cfg.symbol} "
                                    f"while {cfg.symbol} holds {old_amt}."
                                )
                                print(
                                    f"  Flatten {cfg.symbol} first; switching now would leave "
                                    "that position unsupervised. Still running on "
                                    f"{cfg.symbol}."
                                )
                                refusal = {
                                    "action": "config_refused",
                                    "reason": "symbol_change_with_open_position",
                                    "current_symbol": cfg.symbol,
                                    "rejected_symbol": new_cfg.symbol,
                                    "position_amt": old_amt,
                                }
                                journal.log_tick(refusal)
                                notify.config_refused(refusal)
                        else:
                            # Fetch into a temporary: if set_leverage below fails,
                            # the previous symbol's filters stay in force.
                            # Committing filters early would leave cfg on the old
                            # symbol while sizing orders off the new symbol's lot step.
                            new_filters = market.get_filters(api, new_cfg.symbol)
                            market.set_leverage(api, new_cfg.symbol, new_cfg.leverage)
                            filters = new_filters
                            # Zone state belongs to the old symbol's ladder and
                            # means nothing on the new one.
                            active_index = None
                            changes = _config_changes(cfg, new_cfg)
                            cfg = new_cfg
                            halted = False
                            refused_cfg = None
                            print("settings reloaded")
                            reloaded = changes
                    else:
                        if new_cfg.leverage != cfg.leverage:
                            market.set_leverage(api, new_cfg.symbol, new_cfg.leverage)
                        changes = _config_changes(cfg, new_cfg)
                        cfg = new_cfg
                        halted = False
                        refused_cfg = None
                        print("settings reloaded")
                        reloaded = changes
                else:
                    # The file now matches what is running, so any earlier
                    # refusal is spent. Without this, an operator who sets
                    # testnet, reverts it, then sets it again gets NO message
                    # and no journal line the second time - and that loud
                    # message is the entire mitigation. A silent refusal reads
                    # exactly like the hazard it exists to prevent.
                    refused_cfg = None
            # TypeError belongs with the rest: a null or wrong-typed field
            # ("leverage": null) reaches int()/float() and raises it, and a
            # malformed config is a config error, not a generic loop fault.
            # Left out it would escape to the handler at the bottom of the loop
            # and write an unthrottled error record on every single poll.
            except (TypeError, ValueError, KeyError, OSError) as exc:
                # A settings.json left invalid raises here on every poll, so
                # this is throttled like any other sustained state. It gets its
                # own TickLog because the two streams interleave: sharing one
                # would make config_error and the tick's own record alternate,
                # each looking like a change from the last one written, and
                # neither would ever be throttled.
                config_log.log(
                    {"action": "config_error", "error": str(exc)},
                    key=("config_error", str(exc)),
                )

            if reloaded is not None:
                _log_config_reloaded(cfg, reloaded)

            # One read per endpoint for the whole tick: 11 request weight
            # instead of 21, and three instants instead of five. Three
            # sequential round-trips are still three moments - see Snapshot.
            snapshot = market.get_snapshot(api, cfg.symbol)
            price = snapshot.mark_price
            position_amt = snapshot.position_amt
            position_notional = position_amt * price
            wallet = snapshot.wallet_balance
            available = snapshot.available_balance
            pnl = snapshot.unrealized_pnl

            decision = strategy.decide(
                price=price,
                zones=cfg.zones,
                trend=cfg.trend,
                position_notional=position_notional,
                wallet_balance=wallet,
                leverage=cfg.leverage,
                alpha=cfg.alpha,
                exposure_fraction=cfg.exposure_fraction,
                stop_buffer=cfg.stop_buffer,
                rebalance_threshold=cfg.rebalance_threshold,
                min_notional=filters.min_notional,
                active_index=active_index,
            )

            zone = cfg.zones[decision.zone_index] if decision.zone_index is not None else None

            # Re-arm the halt announcement whenever price is back on the
            # ladder. `halted` is what makes HALT announced on the transition
            # into it rather than once per poll for as long as it lasts, but
            # left latched it also silences the NEXT departure - the one that
            # happens after price recovered and the bot took a fresh position.
            if decision.action != strategy.HALT:
                halted = False

            # Whether this tick actually sends an order decides whether its
            # record survives the throttle, so the sizing check - pure
            # arithmetic, no I/O - runs before the journal write. The record
            # still goes in before the order is sent, not after.
            qty = 0.0
            sending = False
            if decision.action not in (strategy.HOLD, strategy.IDLE):
                qty = execution.quantity_for(decision.delta, price, filters)
                sending = execution.is_executable(qty, price, filters)

            tick_log.log(
                {
                    "symbol": cfg.symbol,
                    "mark_price": price,
                    "trend": cfg.trend,
                    "leverage": cfg.leverage,
                    "active_zone_index": decision.zone_index,
                    "support": zone.support if zone else None,
                    "resistance": zone.resistance if zone else None,
                    "d": decision.d,
                    "max_notional": decision.max_n,
                    "target_notional": decision.target_signed,
                    "current_notional": position_notional,
                    "delta": decision.delta,
                    "action": decision.action,
                    "reason": decision.reason,
                    "balance": wallet,
                    "unrealized_pnl": pnl,
                },
                key=(decision.action, decision.reason),
                forced=sending,
            )

            if decision.action in (strategy.HOLD, strategy.IDLE):
                active_index = decision.zone_index
                _sleep(cfg.poll_seconds)
                continue

            if decision.action == strategy.HALT and not halted:
                print(f"HALT - price {price} left the zone ladder")
                halted = True
                # On the transition only, and after this tick's journal write.
                # Sent before the flattening order rather than after it: if
                # execute() raises, the halt is still the thing the operator
                # most needs to hear about, and by then `halted` would have
                # latched and no later tick would say it.
                notify.halt(
                    symbol=cfg.symbol,
                    price=price,
                    trend=cfg.trend,
                    leverage=cfg.leverage,
                    position_amt=position_amt,
                    position_notional=position_notional,
                    flattening=sending,
                )

            if not sending:
                active_index = decision.zone_index
                _sleep(cfg.poll_seconds)
                continue

            side = "BUY" if decision.delta > 0 else "SELL"
            # Flattening orders are sized from the position read at the top of
            # this tick. If it shrank since (partial ADL, manual intervention,
            # another process), a plain market order overshoots and opens the
            # opposite position - a naked short on the very path where the bot
            # decided its long thesis was dead. The gate is direction-based,
            # not reason-based: see execution.should_reduce_only.
            reduce_only = execution.should_reduce_only(
                decision.reason, decision.delta, position_notional
            )
            result = execution.execute(api, cfg.symbol, side, qty, reduce_only=reduce_only)

            # The order is live on the exchange from here. Everything below is
            # bookkeeping, and a failure in it must not lose the fill record or
            # strand active_index -- the order journal is the audit trail and the
            # dataset a learned policy will train on.
            try:
                try:
                    position_after = market.get_position_amt(api, cfg.symbol)
                except Exception as exc:
                    position_after = None
                    journal.log_tick({"action": "error", "error": f"position_after failed: {exc}"})

                # Realised PnL has to be reconstructable from this record, so
                # the fill is recorded as it happened rather than as it was
                # estimated: fill_price/executed_qty come from the RESULT
                # response, mark_price stays as the pre-trade reference, and
                # notional is None rather than a pre-trade guess if the
                # exchange returned no fill data. Commission needs a separate
                # userTrades call that this loop deliberately does not make;
                # the fields are present and null so the gap is explicit.
                fill = execution.fill_from_response(result)

                record = {
                    "order_id": result.get("orderId"),
                    "client_order_id": result.get("clientOrderId"),
                    "symbol": cfg.symbol,
                    "side": side,
                    "reason": decision.reason,
                    "quantity": qty,
                    "executed_qty": fill.qty,
                    "fill_price": fill.price,
                    "notional": fill.notional,
                    "commission": None,
                    "commission_asset": None,
                    "reduce_only": reduce_only,
                    "mark_price": price,
                    "trend": cfg.trend,
                    "leverage": cfg.leverage,
                    "alpha": cfg.alpha,
                    "exposure_fraction": cfg.exposure_fraction,
                    "active_zone_index": decision.zone_index,
                    "support": zone.support if zone else None,
                    "resistance": zone.resistance if zone else None,
                    "d": decision.d,
                    "max_notional": decision.max_n,
                    "target_notional": decision.target_signed,
                    "position_before": position_amt,
                    "position_after": position_after,
                    "balance": wallet,
                    "available_balance": available,
                    "unrealized_pnl": pnl,
                }
                journal.log_order(record)
                print(f"{side} {qty} {cfg.symbol} @ ~{price} ({decision.reason})")
                # Journal first, then Telegram, and from the same record: a
                # notification problem can never cost an order-journal line.
                # Never throttled - every fill moves real money.
                notify.order_executed(record)
            finally:
                active_index = decision.zone_index

        except KeyboardInterrupt:
            print("stopped by operator")
            return
        except Exception as exc:
            # A transient API error must not kill an unattended bot, but it
            # must be recorded rather than swallowed. Nothing encloses this
            # handler, so anything it raises itself - a Windows file lock on
            # the journal it opens every tick, a localised OS error message
            # that the console cannot encode - would propagate out of the loop
            # and end the process holding a position. The handler is therefore
            # built so that it cannot fail: each half is independently
            # guarded, and the text is forced to ASCII before printing.
            #
            # Throttled on the message, because a failing order repeats. With
            # exposure_fraction at 1.0 the required margin exceeds the wallet
            # as d approaches 1, and Binance rejects with -2019 on every poll -
            # right where the strategy wants its largest position. Unthrottled
            # that is one error record per poll on top of the tick's own record.
            # Keyed on the text, so a NEW, different failure is still written
            # the instant it happens; only an identical repeat collapses.
            try:
                message = _ascii(exc)
                error_log.log(
                    {"action": "error", "error": message}, key=("error", message)
                )
            except Exception:
                pass
            try:
                print("ERROR:", _ascii(exc))
                traceback.print_exc()
            except Exception:
                pass

        _sleep(cfg.poll_seconds)


if __name__ == "__main__":
    run()
