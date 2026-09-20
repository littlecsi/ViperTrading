"""Out-of-sample zone derivation, for the run that is not allowed to cheat.

The ladder in futures/test/settings.json was drawn by the operator recently,
with the last month's price action already on the chart. Backtesting it over
that same month is look-ahead bias in its purest form: the support level and
the period's low know about each other. The number that comes out is not
wrong, it just cannot be read as a forecast.

So the backtest runs twice. Run A uses the configured zones and answers "what
would the bot I am running have done". Run B uses zones built here, from data
strictly BEFORE the test window, and answers "does this strategy work when it
cannot see the future". The gap between them is what the hindsight was worth.

The rule below is deliberately dull. A clever derivation tuned until run B
looked good would just relocate the overfitting from the zones into this file,
where it is harder to see.
"""

from settings import Zone


def derive(closes: list[float], count: int = 3) -> tuple[Zone, ...]:
    """`count` contiguous zones spanning the observed range of `closes`.

    Boundaries are evenly spaced percentiles of the price distribution, so
    each zone covers a comparable share of the time price actually spent
    there - rather than an equal slice of the range, which would hand the
    thinly-visited extremes the same weight as the middle.

    Contiguity is required, not cosmetic: strategy.select_zone leans on one
    zone's support being the next one's resistance, and uses stop_buffer as
    the hysteresis dead band around that shared edge. Zones with gaps between
    them would leave price in no zone at all and idle the bot; overlapping
    ones would make zone selection order-dependent.

    Returned highest-first, matching settings.load()'s own ordering."""
    if len(closes) < count + 1:
        raise ValueError(f"need more than {count} closes to derive {count} zones")

    ordered = sorted(closes)
    last = len(ordered) - 1

    # count + 1 boundaries, low to high, at evenly spaced percentiles.
    edges = []
    for i in range(count + 1):
        pos = (len(ordered) - 1) * i / count
        lo = int(pos)
        hi = min(lo + 1, last)
        frac = pos - lo
        edges.append(ordered[lo] * (1 - frac) + ordered[hi] * frac)

    # Strictly increasing, or Zone() rejects support >= resistance. A flat
    # window (every close identical) is the degenerate case.
    for i in range(1, len(edges)):
        if edges[i] <= edges[i - 1]:
            edges[i] = edges[i - 1] * 1.0001

    zones = [Zone(support=edges[i], resistance=edges[i + 1]) for i in range(count)]
    return tuple(reversed(zones))
