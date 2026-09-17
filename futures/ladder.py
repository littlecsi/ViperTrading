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


def liquidation_scale(
    position_qty: float,
    entry_price: float,
    isolated_wallet: float,
    leverage: int,
    tier,  # market.MarginTier
    survival_price: float,
    planned_delta_qty: float,
    planned_delta_notional: float,
    trend: str,
) -> float:
    """Safety factor k in [0, 1] to scale the remaining accumulate-side
    rungs by, so that filling ALL of them at their planned (unscaled) sizes
    would not push the projected liquidation price past survival_price.

    Uses Binance's documented isolated-margin liquidation formula (single
    position, one-way mode); adding delta_qty at price p is modelled as also
    adding delta_qty*p/leverage of fresh isolated margin, matching how
    Binance funds an added fill at a fixed leverage.

    Safety is evaluated at the two endpoints k=0 and k=1 FIRST, rather than
    solving the closed form and clamping the root into range. Because the
    denominator qty*(1 -/+ mmr) is positive, "liquidation price stays the
    right side of survival_price" is equivalent to the linear test
    (a - c*position_qty) + (b - c*planned_delta_qty) * k <= 0. The SIGN of
    that slope (b - c*planned_delta_qty) is not fixed by the inputs, so the
    safe region is sometimes [0, k*] and sometimes [k*, 1] -- knowing the
    root k* tells you nothing on its own about which side of it is safe, and
    a root outside [0, 1] can mean either "everything here is safe" or
    "nothing here is". Checking the endpoints resolves that first: if they
    disagree the root is the crossing between a known-safe and a known-unsafe
    end, which is the only case where solving is meaningful. k=1 means the
    full planned accumulation is safe as-is; k=0 means even the smallest
    further accumulation is unsafe. See the design doc's "Liquidation-aware
    buy-side cap"."""
    mmr = tier.maint_margin_rate
    maint_amount = tier.maint_amount

    def liquidation_price(k: float) -> float:
        qty = position_qty + k * planned_delta_qty
        if qty == 0:
            # A zero position cannot be liquidated, so there is nothing to
            # guard at this k: report a liquidation price infinitely far in
            # the adverse direction, which safe() then reads as safe. This is
            # reachable in normal operation -- a round trip back to flat
            # inside an active zone re-enters with position_qty == 0, and the
            # k=0 endpoint is evaluated whenever k=1 is not already safe --
            # and the only alternative here is dividing by zero.
            return float("-inf") if trend == LONG else float("inf")
        added_wallet = k * planned_delta_notional / leverage
        cost_basis = entry_price * position_qty + k * planned_delta_notional
        if trend == LONG:
            numerator = cost_basis - (isolated_wallet + added_wallet) + maint_amount
            return numerator / (qty * (1 - mmr))
        numerator = cost_basis + (isolated_wallet + added_wallet) - maint_amount
        return numerator / (qty * (1 + mmr))

    def safe(price: float) -> bool:
        return price <= survival_price if trend == LONG else price >= survival_price

    if safe(liquidation_price(1.0)):
        return 1.0
    if not safe(liquidation_price(0.0)):
        return 0.0

    # k=1 is unsafe and k=0 reads as safe, so solve for the crossing point.
    # For position_qty > 0 that crossing is genuinely inside (0, 1). For a
    # flat position it may not be: the k=0 end is only safe via the
    # zero-position sentinel above, not on the real curve, so the real curve
    # can fail to cross at all within [0, 1] -- which is why the root is
    # range-checked rather than clamped below.
    if trend == LONG:
        a = entry_price * position_qty - isolated_wallet + maint_amount
        b = planned_delta_notional * (1 - 1 / leverage)
        c = survival_price * (1 - mmr)
    else:
        a = entry_price * position_qty + isolated_wallet - maint_amount
        b = planned_delta_notional * (1 + 1 / leverage)
        c = survival_price * (1 + mmr)

    denominator = b - c * planned_delta_qty
    if denominator == 0:
        # Safety reduces to the linear test (a - c*position_qty) + denominator
        # * k <= 0, so a zero denominator makes it independent of k -- which
        # the endpoint checks above would already have caught, EXCEPT when
        # position_qty == 0 put a discontinuity at k=0 (flat is trivially
        # safe, nothing above it is). Nothing beyond zero is safe there.
        return 0.0

    # Range-check, never clamp. Clamping a root above 1 up to 1.0 would
    # report "the whole plan is safe" for a plan whose k=1 endpoint was just
    # measured unsafe. Reaching here always means the real k=1 is unsafe, so
    # a root outside [0, 1] means the curve never crosses survival_price
    # inside the interval and the unsafe sign holds across all of it: cap the
    # accumulate side to nothing. (A NaN root fails this test too, and lands
    # on the same conservative answer.)
    k = (c * position_qty - a) / denominator
    return k if 0.0 <= k <= 1.0 else 0.0


def survival_price(
    zones: tuple[Zone, ...],
    active_index: int,
    trend: str,
    liquidation_buffer_pct: float,
) -> float:
    """The price the position must survive to without liquidating.

    Zones are contiguous and ordered highest-to-lowest (settings.py already
    enforces zones[i].support == zones[i+1].resistance), so for LONG the
    next-lower zone is at active_index + 1. At the ladder's lowest zone
    (LONG) or highest zone (SHORT), where there is no next zone in that
    direction, the same buffer percentage is applied to the active zone's
    own span instead. See design doc "Liquidation-aware buy-side cap"."""
    active = zones[active_index]
    if trend == LONG:
        if active_index + 1 < len(zones):
            next_zone = zones[active_index + 1]
            span = next_zone.resistance - next_zone.support
            return next_zone.resistance - liquidation_buffer_pct * span
        span = active.resistance - active.support
        return active.support - liquidation_buffer_pct * span
    else:
        if active_index - 1 >= 0:
            next_zone = zones[active_index - 1]
            span = next_zone.resistance - next_zone.support
            return next_zone.support + liquidation_buffer_pct * span
        span = active.resistance - active.support
        return active.resistance + liquidation_buffer_pct * span


def apply_liquidation_cap(
    orders: tuple[DesiredOrder, ...], scale: float, trend: str
) -> tuple[DesiredOrder, ...]:
    """Scale down only the accumulate-side orders (BUY for long, SELL for
    short) by `scale`; the trim side is untouched. `scale` comes from
    liquidation_scale()."""
    accumulate_side = "BUY" if trend == LONG else "SELL"
    return tuple(
        DesiredOrder(price=o.price, side=o.side, size=o.size * scale)
        if o.side == accumulate_side
        else o
        for o in orders
    )
