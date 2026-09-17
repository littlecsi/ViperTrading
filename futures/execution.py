import math
from dataclasses import dataclass

from binance.error import ClientError

from market import Filters
from strategy import HALT_FLATTEN, STOP_OUT


@dataclass(frozen=True)
class Fill:
    """What an order actually did, as opposed to what it was asked to do.

    Every field is optional because the exchange response is the only source
    here: when it carries no fill data the journal must say so rather than
    fall back to a pre-trade estimate dressed up as a fill."""

    price: float | None
    qty: float | None
    notional: float | None


def quantity_for(delta_notional: float, price: float, filters: Filters) -> float:
    """Absolute order quantity for a notional delta, floored to the lot step.

    Always rounds DOWN so the bot cannot overshoot its own exposure cap."""
    raw = abs(delta_notional) / price
    steps = math.floor(raw / filters.step_size)
    qty = steps * filters.step_size
    # floor() on binary floats leaves trailing noise; the lot step has at most
    # 8 decimals, so rounding there is exact without reintroducing overshoot.
    return round(qty, 8)


def price_for(price: float, filters: Filters, side: str) -> float:
    """Limit-order price rounded to the exchange's tick size, away from the
    market on both sides.

    Mirrors quantity_for()'s rounding rationale: the exchange rejects a price
    that is not an exact multiple of tickSize with -1111, and floats
    accumulate noise that a naive round() can push onto an invalid tick. The
    rounding DIRECTION must be side-aware, not a blanket floor: a BUY rung
    sits below current price, so flooring moves it further below (away from
    market) and can only ever be safe; a SELL rung sits above current price,
    so flooring it would move it DOWN, toward and potentially past the
    market, turning an intended resting order into an unintended marketable
    (taker) fill. SELL therefore ceilings instead, moving it further above
    (away from market) - the mirror image of BUY, both directions chosen so
    rounding can never make a resting order more aggressive than intended."""
    ticks = round(price / filters.tick_size, 8)
    rounded = math.ceil(ticks) if side == "SELL" else math.floor(ticks)
    return round(rounded * filters.tick_size, 8)


def is_executable(qty: float, price: float, filters: Filters) -> bool:
    if qty <= 0 or qty < filters.min_qty:
        return False
    return qty * price >= filters.min_notional


def should_reduce_only(reason, delta: float, position_notional: float) -> bool:
    """Whether an order may be sent reduceOnly.

    The tempting test - "is this a stop-out or a halt-flatten?" - is wrong, and
    wrong in a way that wedges the bot. STOP_OUT does not mean flatten: the
    strategy assigns it whenever the active zone changed while a position is
    held, regardless of the new target's size. Crossing a zone boundary UPWARD
    lands price near the new zone's support, where d approaches 1 and the
    target approaches max_notional, so a STOP_OUT is routinely a large
    position-INCREASING order. Tagged reduceOnly the exchange rejects it
    (-2022), and since execute() raises before the tick updates active_index,
    the next tick recomputes the same STOP_OUT and is rejected again: an
    unbounded rejected-order loop, one per poll interval, with the strategy
    permanently unable to enter the new zone.

    Direction is the only sound test. A reducing order is by definition one
    that opposes the position it is sent against, which still delivers what
    reduceOnly was added for - an order sized against a position that has since
    shrunk can no longer overshoot into a position on the opposite side."""
    if reason not in (STOP_OUT, HALT_FLATTEN):
        return False
    if position_notional == 0:
        # reduceOnly is rejected outright when there is nothing to reduce.
        return False
    return (delta > 0) != (position_notional > 0)


def _positive(value) -> float | None:
    """Parse a numeric response field, treating absent, unparseable and zero
    alike: Binance reports "0.00" for fill fields it has not computed yet."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def fill_from_response(result: dict) -> Fill:
    """Extract the realised fill from a new_order response.

    avgPrice is the direct answer, but a futures MARKET response can come back
    with executedQty set and avgPrice still "0.00"; cumQuote (the cumulative
    quote quantity) then carries the true notional and implies the average
    price. Anything still unknown stays None - the alternative, quietly
    substituting the pre-trade mark price, is what made the old records
    understate trading cost."""
    price = _positive(result.get("avgPrice"))
    qty = _positive(result.get("executedQty"))
    notional = _positive(result.get("cumQuote"))

    if notional is None and price is not None and qty is not None:
        notional = price * qty
    if price is None and notional is not None and qty is not None:
        price = notional / qty

    return Fill(price=price, qty=qty, notional=notional)


def execute(
    client,
    symbol: str,
    side: str,
    qty: float,
    reduce_only: bool = False,
) -> dict:
    """Send a market order. Raises on failure - a rejected order must surface
    rather than be silently swallowed.

    newOrderRespType="RESULT" is what makes the order journal able to
    reconstruct realised PnL: the default "ACK" response carries no avgPrice or
    executedQty, so the fill data would have to be re-fetched or guessed from
    the pre-trade mark price.

    reduce_only marks an order that may only shrink the position. Flattening
    orders are sized from a position read earlier in the tick; if that position
    shrank in between, a plain market order would overshoot and open a position
    in the opposite direction. Note the exchange REJECTS a reduceOnly order
    when there is nothing to reduce, so only set it against a live position."""
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
        "newOrderRespType": "RESULT",
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return client.new_order(**params)


def place_limit_order(
    client,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    reduce_only: bool = False,
) -> dict:
    """Place a resting GTC limit order. Used only for in-zone SCALE_IN/
    SCALE_OUT rungs -- STOP_OUT/HALT_FLATTEN/dead-band exits keep using
    execute() (market), unchanged, per the design doc."""
    params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "quantity": qty,
        "price": price,
        "timeInForce": "GTC",
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return client.new_order(**params)


def cancel_orders(client, symbol: str, order_ids) -> list[dict]:
    """Cancel each id, tolerating one already gone (-2011) rather than
    raising: the bot's tracked rung state is read once a tick and can be one
    poll stale relative to the exchange, e.g. if a rung filled between the
    diff read and the cancel call."""
    results = []
    for order_id in order_ids:
        try:
            results.append(client.cancel_order(symbol=symbol, orderId=order_id))
        except ClientError as exc:
            if exc.error_code == -2011:
                results.append({"orderId": order_id, "status": "already_gone"})
            else:
                raise
    return results


def cancel_all_orders(client, symbol: str) -> dict:
    """Cancel EVERY resting order on `symbol`, in one request.

    DELETE /fapi/v1/allOpenOrders: one round-trip and one unit of request
    weight however many rungs are on the book. cancel_orders() above is one
    BLOCKING round-trip per order - nineteen of them for this repo's own zone
    geometry - which is the wrong shape for tearing a ladder down. The exit
    path pays that latency immediately before a market order that must not
    wait, and a nineteen-request burst is itself a good way to get rate
    limited, which per the design notes means an IP ban while holding a
    leveraged position the bot then cannot flatten.

    Every ladder teardown wants this one: they all abandon the zone's whole
    order set, none of them keeps some rungs and cancels others. It also
    sweeps up orders this process never tracked - rungs left resting by a
    crash before their ids were recorded. cancel_orders() stays for
    reconciliation, which is the selective case."""
    return client.cancel_open_orders(symbol=symbol)


def query_order_result(client, symbol: str, order_id) -> dict:
    """Raw exchange response for one order, used to tell a filled rung from
    a cancelled one and to extract its fill via fill_from_response()."""
    return client.query_order(symbol=symbol, orderId=order_id)
