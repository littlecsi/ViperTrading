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
