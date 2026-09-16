import math

from market import Filters


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


def execute(client, symbol: str, side: str, qty: float) -> dict:
    """Send a market order. Raises on failure — a rejected order must surface
    rather than be silently swallowed."""
    return client.new_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
