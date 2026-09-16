import time
import traceback

import client
import execution
import journal
import market
import settings
import strategy


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
    start_price = market.get_mark_price(api, cfg.symbol)
    start_amt = market.get_position_amt(api, cfg.symbol)
    start_notional = start_amt * start_price
    start_max_n = strategy.max_notional(
        market.get_wallet_balance(api), cfg.leverage, cfg.exposure_fraction
    )
    wrong_side = (cfg.trend == settings.LONG and start_notional < 0) or (
        cfg.trend == settings.SHORT and start_notional > 0
    )
    if wrong_side or abs(start_notional) > start_max_n:
        problem = "opposes trend" if wrong_side else "exceeds the exposure cap"
        print(f"REFUSING TO START: existing {cfg.symbol} position {problem}.")
        print(f"  position: {start_amt} ({start_notional:.2f} USDT at {start_price})")
        print(f"  trend: {cfg.trend}, max notional this bot would hold: {start_max_n:.2f} USDT")
        print("  Flatten or reconcile that position manually, then start the bot again.")
        journal.log_tick(
            {
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
        )
        return

    active_index = None
    halted = False
    # The config whose reload was last refused. Refusals are re-evaluated every
    # tick (the operator may flatten, or edit the file again), but only
    # announced when the refused config changes, so a walked-away operator does
    # not come back to 17k identical lines.
    refused_cfg = None

    while True:
        try:
            # Re-read settings each tick so trend, leverage, and zones can be
            # changed without a restart. Invalid edits are ignored, not fatal.
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
                            journal.log_tick(
                                {
                                    "action": "config_refused",
                                    "reason": "testnet_change_requires_restart",
                                    "current_testnet": cfg.testnet,
                                    "rejected_testnet": new_cfg.testnet,
                                }
                            )
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
                                journal.log_tick(
                                    {
                                        "action": "config_refused",
                                        "reason": "symbol_change_with_open_position",
                                        "current_symbol": cfg.symbol,
                                        "rejected_symbol": new_cfg.symbol,
                                        "position_amt": old_amt,
                                    }
                                )
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
                            cfg = new_cfg
                            halted = False
                            refused_cfg = None
                            print("settings reloaded")
                    else:
                        if new_cfg.leverage != cfg.leverage:
                            market.set_leverage(api, new_cfg.symbol, new_cfg.leverage)
                        cfg = new_cfg
                        halted = False
                        refused_cfg = None
                        print("settings reloaded")
                else:
                    # The file now matches what is running, so any earlier
                    # refusal is spent. Without this, an operator who sets
                    # testnet, reverts it, then sets it again gets NO message
                    # and no journal line the second time - and that loud
                    # message is the entire mitigation. A silent refusal reads
                    # exactly like the hazard it exists to prevent.
                    refused_cfg = None
            except (ValueError, KeyError, OSError) as exc:
                journal.log_tick({"action": "config_error", "error": str(exc)})

            price = market.get_mark_price(api, cfg.symbol)
            position_amt = market.get_position_amt(api, cfg.symbol)
            position_notional = position_amt * price
            wallet = market.get_wallet_balance(api)
            available = market.get_available_balance(api)
            pnl = market.get_unrealized_pnl(api, cfg.symbol)

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

            journal.log_tick(
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
                }
            )

            if decision.action in (strategy.HOLD, strategy.IDLE):
                active_index = decision.zone_index
                _sleep(cfg.poll_seconds)
                continue

            if decision.action == strategy.HALT and not halted:
                print(f"HALT - price {price} left the zone ladder")
                halted = True

            qty = execution.quantity_for(decision.delta, price, filters)
            if not execution.is_executable(qty, price, filters):
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

                journal.log_order(
                    {
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
                )
                print(f"{side} {qty} {cfg.symbol} @ ~{price} ({decision.reason})")
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
            try:
                journal.log_tick({"action": "error", "error": _ascii(exc)})
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
