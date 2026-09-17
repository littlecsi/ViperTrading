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

from settings import Zone, LONG, SHORT


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
