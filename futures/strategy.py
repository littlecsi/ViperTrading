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
