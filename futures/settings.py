import json
from dataclasses import dataclass
from pathlib import Path

SETTINGS_PATH = str(Path(__file__).resolve().parent / "settings.json")

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
    testnet: bool
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


def load(path: str = SETTINGS_PATH) -> Settings:
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

    return Settings(
        symbol=str(data["symbol"]),
        trend=trend,
        leverage=leverage,
        alpha=alpha,
        exposure_fraction=exposure_fraction,
        rebalance_threshold=float(data["rebalance_threshold"]),
        stop_buffer=float(data["stop_buffer"]),
        poll_seconds=int(data["poll_seconds"]),
        testnet=bool(data["testnet"]),
        zones=_parse_zones(data["zones"]),
    )
