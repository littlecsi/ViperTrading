import math
from dataclasses import dataclass

from market import Filters


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


def is_executable(qty: float, price: float, filters: Filters) -> bool:
    if qty <= 0 or qty < filters.min_qty:
        return False
    return qty * price >= filters.min_notional


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
