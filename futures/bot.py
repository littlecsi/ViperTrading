import time
import traceback

import client
import execution
import journal
import market
import settings
import strategy


def run() -> None:
    cfg = settings.load()
    api = client.build(cfg.testnet)

    market.set_leverage(api, cfg.symbol, cfg.leverage)
    filters = market.get_filters(api, cfg.symbol)

    label = "TESTNET" if cfg.testnet else "LIVE"
    print(f"Viper starting on {label} - {cfg.symbol} @ {cfg.leverage}x, trend={cfg.trend}")

    active_index = None
    halted = False

    while True:
        try:
            # Re-read settings each tick so trend, leverage, and zones can be
            # changed without a restart. Invalid edits are ignored, not fatal.
            try:
                new_cfg = settings.load()
                if new_cfg != cfg:
                    if new_cfg.symbol != cfg.symbol:
                        # Fetch into a temporary: if set_leverage below fails,
                        # the previous symbol's filters stay in force.
                        # Committing filters early would leave cfg on the old
                        # symbol while sizing orders off the new symbol's lot step.
                        new_filters = market.get_filters(api, new_cfg.symbol)
                        market.set_leverage(api, new_cfg.symbol, new_cfg.leverage)
                        filters = new_filters
                    elif new_cfg.leverage != cfg.leverage:
                        market.set_leverage(api, new_cfg.symbol, new_cfg.leverage)
                    cfg = new_cfg
                    halted = False
                    print("settings reloaded")
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
                time.sleep(cfg.poll_seconds)
                continue

            if decision.action == strategy.HALT and not halted:
                print(f"HALT - price {price} left the zone ladder")
                halted = True

            qty = execution.quantity_for(decision.delta, price, filters)
            if not execution.is_executable(qty, price, filters):
                active_index = decision.zone_index
                time.sleep(cfg.poll_seconds)
                continue

            side = "BUY" if decision.delta > 0 else "SELL"
            result = execution.execute(api, cfg.symbol, side, qty)

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

                journal.log_order(
                    {
                        "order_id": result.get("orderId"),
                        "client_order_id": result.get("clientOrderId"),
                        "symbol": cfg.symbol,
                        "side": side,
                        "reason": decision.reason,
                        "quantity": qty,
                        "notional": qty * price,
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
            # must be recorded rather than swallowed.
            journal.log_tick({"action": "error", "error": str(exc)})
            print("ERROR:", exc)
            traceback.print_exc()

        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    run()
