from dataclasses import dataclass

from settings import Zone, LONG, SHORT


def max_notional(wallet_balance: float, leverage: int, exposure_fraction: float) -> float:
    """Maximum position notional. Uses TOTAL wallet balance, never available
    balance: available balance shrinks as margin is consumed, which would
    shrink the target as the position fills and stall accumulation."""
    return wallet_balance * leverage * exposure_fraction


def distance(price: float, zone: Zone, trend: str) -> float:
    """Normalised distance to the favourable level, clamped to [0, 1].
    1.0 means price is at the level the bot accumulates into."""
    span = zone.resistance - zone.support
    if trend == LONG:
        d = (zone.resistance - price) / span
    else:
        d = (price - zone.support) / span
    return min(1.0, max(0.0, d))


def target_notional(d: float, max_n: float, alpha: float) -> float:
    return max_n * (d ** alpha)


def signed(target: float, trend: str) -> float:
    return target if trend == LONG else -target


def select_zone(
    price: float,
    zones: tuple[Zone, ...],
    active_index: int | None,
    stop_buffer: float,
) -> int | None:
    """Index of the zone the bot should work, or None if price is off the ladder.

    An already-active zone is retained until price leaves it by stop_buffer.
    This dead band matters because contiguous zones share a boundary: without
    it, price hovering on that boundary would flip the target between maximum
    and flat on every tick."""
    if active_index is not None and 0 <= active_index < len(zones):
        z = zones[active_index]
        if z.support * (1 - stop_buffer) <= price <= z.resistance * (1 + stop_buffer):
            return active_index

    for i, z in enumerate(zones):
        if z.support <= price <= z.resistance:
            return i

    return None


def past_adverse_end(
    price: float,
    zones: tuple[Zone, ...],
    trend: str,
    stop_buffer: float,
) -> bool:
    """True when price has left the ladder in the direction that invalidates
    the operator's thesis entirely — below every support when long, above
    every resistance when short."""
    if trend == LONG:
        return price < min(z.support for z in zones) * (1 - stop_buffer)
    return price > max(z.resistance for z in zones) * (1 + stop_buffer)


HOLD = "hold"
BUY = "buy"
SELL = "sell"
HALT = "halt"
IDLE = "idle"

SCALE_IN = "scale_in"
SCALE_OUT = "scale_out"
STOP_OUT = "stop_out"
HALT_FLATTEN = "halt_flatten"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str | None
    zone_index: int | None
    d: float | None
    max_n: float
    target_signed: float
    delta: float


def decide(
    price: float,
    zones: tuple[Zone, ...],
    trend: str,
    position_notional: float,
    wallet_balance: float,
    leverage: int,
    alpha: float,
    exposure_fraction: float,
    stop_buffer: float,
    rebalance_threshold: float,
    min_notional: float,
    active_index: int | None,
) -> Decision:
    """Pure decision step. Plain values in, target position out.

    This is the seam a learned policy replaces: nothing here touches the
    network, the filesystem, or the clock."""
    max_n = max_notional(wallet_balance, leverage, exposure_fraction)

    if past_adverse_end(price, zones, trend, stop_buffer):
        return Decision(
            action=HALT,
            reason=HALT_FLATTEN if position_notional != 0 else None,
            zone_index=None,
            d=None,
            max_n=max_n,
            target_signed=0.0,
            delta=-position_notional,
        )

    zone_index = select_zone(price, zones, active_index, stop_buffer)

    if zone_index is None:
        if position_notional == 0:
            action, reason = IDLE, None
        else:
            action = SELL if position_notional > 0 else BUY
            reason = SCALE_OUT
        return Decision(
            action=action,
            reason=reason,
            zone_index=None,
            d=None,
            max_n=max_n,
            target_signed=0.0,
            delta=-position_notional,
        )

    d = distance(price, zones[zone_index], trend)
    target = signed(target_notional(d, max_n, alpha), trend)
    delta = target - position_notional

    threshold = max(rebalance_threshold * max_n, min_notional)
    if abs(delta) < threshold:
        return Decision(
            action=HOLD,
            reason=None,
            zone_index=zone_index,
            d=d,
            max_n=max_n,
            target_signed=target,
            delta=0.0,
        )

    if active_index is not None and zone_index != active_index and position_notional != 0:
        reason = STOP_OUT
    elif abs(target) > abs(position_notional):
        reason = SCALE_IN
    else:
        reason = SCALE_OUT

    return Decision(
        action=BUY if delta > 0 else SELL,
        reason=reason,
        zone_index=zone_index,
        d=d,
        max_n=max_n,
        target_signed=target,
        delta=delta,
    )
