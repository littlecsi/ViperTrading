"""Pure limit-order ladder computation for in-zone SCALE_IN/SCALE_OUT.

Kept separate from strategy.py rather than folded into it: strategy.decide()
is the seam a future PPO policy replaces, and that policy will still output
a continuous target the same way it does today. This module is what turns
that continuous target into discrete resting orders -- mixing the two would
put order-placement concerns behind the interface the policy replaces.

No I/O, no client, no clock -- see the design doc
docs/superpowers/specs/2026-09-17-limit-order-ladder-design.md for the full
rationale and the "Rung table" / "Liquidation-aware buy-side cap" derivations
this module implements.
"""

import math
from dataclasses import dataclass

from settings import Zone, LONG, SHORT
from strategy import distance, target_notional, signed


def rung_prices(zone: Zone, rung_spacing_pct: float) -> tuple[float, ...]:
    """Evenly spaced prices from support to resistance, inclusive.

    Spacing is a fraction of the zone's resistance (not the span), so a
    fixed rung_spacing_pct gives roughly consistent absolute spacing across
    zones of different width relative to price -- see design doc "Rung
    table". A zone narrower than one spacing step still gets one rung
    interval (count is floored at 1) rather than degenerating to zero."""
    span = zone.resistance - zone.support
    count = max(1, math.ceil(span / (rung_spacing_pct * zone.resistance)))
    step = span / count
    return tuple(zone.support + step * i for i in range(count + 1))


@dataclass(frozen=True)
class Rung:
    price: float
    cumulative_target: float  # signed target notional if price sat here


def rung_table(
    zone: Zone,
    trend: str,
    max_n: float,
    alpha: float,
    rung_spacing_pct: float,
) -> tuple[Rung, ...]:
    """Cumulative signed target at each rung price, reusing strategy.py's
    existing curve unchanged -- sampled at fixed points instead of
    continuously. See design doc "Rung table"."""
    rungs = []
    for price in rung_prices(zone, rung_spacing_pct):
        d = distance(price, zone, trend)
        target = signed(target_notional(d, max_n, alpha), trend)
        rungs.append(Rung(price=price, cumulative_target=target))
    return tuple(rungs)


@dataclass(frozen=True)
class RungOrder:
    price: float
    size: float  # non-negative notional magnitude


def build_rung_orders(
    zone: Zone,
    trend: str,
    max_n: float,
    alpha: float,
    rung_spacing_pct: float,
) -> tuple[RungOrder, ...]:
    """One order per rung except the unfavourable edge (target is exactly 0
    there, and that boundary already belongs to STOP_OUT/the dead-band
    exit). Size is this rung's slice of the target curve -- the delta to its
    neighbour on the unfavourable side -- which is why sell-side sizes near
    that edge come out smaller than buy-side sizes near the favourable edge:
    it falls directly out of the existing alpha curve. See design doc "Rung
    table"."""
    points = rung_table(zone, trend, max_n, alpha, rung_spacing_pct)
    orders = []
    if trend == LONG:
        # resistance (last point) is the unfavourable edge; no order there.
        for i in range(len(points) - 1):
            size = abs(points[i].cumulative_target - points[i + 1].cumulative_target)
            orders.append(RungOrder(price=points[i].price, size=size))
    else:
        # support (first point) is the unfavourable edge; no order there.
        for i in range(1, len(points)):
            size = abs(points[i].cumulative_target - points[i - 1].cumulative_target)
            orders.append(RungOrder(price=points[i].price, size=size))
    return tuple(orders)


def side_for(rung_price: float, current_price: float, trend: str) -> str:
    """BUY or SELL for a resting order at rung_price, given current market
    price. For LONG, below current price accumulates (BUY); for SHORT,
    accumulating (growing the short) happens as price rises toward the
    favourable resistance, so the mapping mirrors. See design doc "Rung
    table"."""
    if trend == LONG:
        return "BUY" if rung_price < current_price else "SELL"
    return "SELL" if rung_price > current_price else "BUY"


@dataclass(frozen=True)
class DesiredOrder:
    price: float
    side: str
    size: float


def desired_orders(
    zone: Zone,
    trend: str,
    max_n: float,
    alpha: float,
    rung_spacing_pct: float,
    current_price: float,
) -> tuple[DesiredOrder, ...]:
    """The full set of resting orders the ladder wants right now, purely a
    function of the zone and current price -- no fill history needed. A
    rung's role flips between accumulate and trim automatically as price
    moves, because side_for() only looks at where current price sits."""
    return tuple(
        DesiredOrder(price=rung.price, side=side_for(rung.price, current_price, trend), size=rung.size)
        for rung in build_rung_orders(zone, trend, max_n, alpha, rung_spacing_pct)
    )
