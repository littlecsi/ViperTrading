import json
from dataclasses import dataclass
from pathlib import Path

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class Zone:
    support: float
    resistance: float


@dataclass(frozen=True)
class Settings:
    symbol: str
    trend: str
    leverage: int
    alpha: float
    exposure_fraction: float
    rebalance_threshold: float
    stop_buffer: float
    poll_seconds: int
    rung_spacing_pct: float
    liquidation_buffer_pct: float
    zones: tuple[Zone, ...]


def _parse_zones(raw) -> tuple[Zone, ...]:
    if not raw:
        raise ValueError("zones must contain at least one entry")

    zones = []
    for i, z in enumerate(raw):
        support = float(z["support"])
        resistance = float(z["resistance"])
        if support >= resistance:
            raise ValueError(f"zone {i}: support must be below resistance")
        zones.append(Zone(support=support, resistance=resistance))

    for i in range(1, len(zones)):
        if zones[i].resistance > zones[i - 1].support:
            raise ValueError("zones must be in descending order and must not overlap")

    return tuple(zones)


def load(path: str) -> Settings:
    """Read and validate one mode's settings.json.

    `path` is required, and comes from env.settings_path(MODE). There is no
    default: each mode owns a file under its own folder, so a caller that
    passes nothing has not said which environment it means.

    Note what is NOT read here: which exchange to trade. That is env.MODE, a
    constant in bot.py. This file is re-read every tick, and a selector that
    can change under a running bot is one that can swap accounts while a
    leveraged position is open. A "testnet" key left over in an operator's old
    file is therefore ignored rather than honoured - silently, because failing
    the load would take the whole configuration down over a dead key."""
    data = json.loads(Path(path).read_text())

    trend = data["trend"]
    if trend not in (LONG, SHORT):
        raise ValueError(f"trend must be '{LONG}' or '{SHORT}', got {trend!r}")

    leverage = int(data["leverage"])
    if leverage <= 0:
        raise ValueError("leverage must be positive")

    exposure_fraction = float(data["exposure_fraction"])
    if not 0 < exposure_fraction <= 1:
        raise ValueError("exposure_fraction must be in (0, 1]")

    alpha = float(data["alpha"])
    if alpha <= 0:
        raise ValueError("alpha must be positive")

    # The three below are hot-reloaded into a running bot, so a typo here is a
    # live-fire hazard rather than a startup nuisance: poll_seconds feeds
    # time.sleep (negative raises, zero busy-loops into a rate-limit ban), and
    # a rebalance_threshold of 0 collapses the churn guard onto the exchange
    # min-notional floor, which bleeds fees on nearly every tick.
    rebalance_threshold = float(data["rebalance_threshold"])
    if not 0 < rebalance_threshold < 1:
        raise ValueError("rebalance_threshold must be in (0, 1)")

    stop_buffer = float(data["stop_buffer"])
    if not 0 <= stop_buffer < 1:
        raise ValueError("stop_buffer must be in [0, 1)")

    rung_spacing_pct = float(data["rung_spacing_pct"])
    if not 0 < rung_spacing_pct < 1:
        raise ValueError("rung_spacing_pct must be in (0, 1)")

    liquidation_buffer_pct = float(data["liquidation_buffer_pct"])
    if not 0 <= liquidation_buffer_pct < 1:
        raise ValueError("liquidation_buffer_pct must be in [0, 1)")

    poll_seconds = int(data["poll_seconds"])
    if poll_seconds < 1:
        raise ValueError("poll_seconds must be at least 1")

    return Settings(
        symbol=str(data["symbol"]),
        trend=trend,
        leverage=leverage,
        alpha=alpha,
        exposure_fraction=exposure_fraction,
        rebalance_threshold=rebalance_threshold,
        stop_buffer=stop_buffer,
        poll_seconds=poll_seconds,
        rung_spacing_pct=rung_spacing_pct,
        liquidation_buffer_pct=liquidation_buffer_pct,
        zones=_parse_zones(data["zones"]),
    )
