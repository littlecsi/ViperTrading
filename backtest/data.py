"""Historical market data for the backtest: 1m klines and funding rates.

LIVE market data, never testnet. Binance's testnet is a separate, thin venue
whose prices diverge materially from the real one - at the time this was
written live ETHUSDT was 2587.70 while testnet showed 2623.16. Backtesting
against testnet history would be backtesting against fiction.

Everything is cached to backtest/data/ on first fetch. Re-running a backtest
must not re-download 43,200 bars, and a cached file also makes a result
reproducible: the same run over the same window gives the same answer even if
the exchange is unreachable later.

Public endpoints only. No credentials are read here and none are needed -
klines and funding history are not account data.
"""

import json
import time
from pathlib import Path

from binance.um_futures import UMFutures

DATA_DIR = Path(__file__).resolve().parent / "data"

# Binance's per-request ceiling. 43,200 one-minute bars is 29 requests.
_MAX_BARS = 1500
_MINUTE_MS = 60_000


def _client() -> UMFutures:
    """Unauthenticated, live. Not client.build() - that one picks an ACCOUNT,
    and this module wants the public market, which has no account."""
    return UMFutures()


def _cache_path(kind: str, symbol: str, start_ms: int, end_ms: int) -> Path:
    return DATA_DIR / f"{kind}-{symbol}-{start_ms}-{end_ms}.json"


def _cached(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        # A truncated cache file is worth less than the seconds it saves.
        return None


def _store(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def fetch_klines(symbol: str, start_ms: int, end_ms: int, interval: str = "1m") -> list[dict]:
    """Every `interval` bar in [start_ms, end_ms), oldest first.

    Paged on the OPEN TIME of the last bar received plus one interval, rather
    than on a request count: Binance can return fewer bars than asked for, and
    a count-based loop silently drops the remainder of a window when it does."""
    path = _cache_path(f"klines-{interval}", symbol, start_ms, end_ms)
    cached = _cached(path)
    if cached is not None:
        return cached

    api = _client()
    bars, cursor = [], start_ms
    while cursor < end_ms:
        batch = api.klines(symbol=symbol, interval=interval,
                           startTime=cursor, endTime=end_ms, limit=_MAX_BARS)
        if not batch:
            break
        for raw in batch:
            bars.append({
                "open_time": int(raw[0]),
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low": float(raw[3]),
                "close": float(raw[4]),
                "volume": float(raw[5]),
            })
        cursor = int(batch[-1][0]) + _MINUTE_MS
        if len(batch) < _MAX_BARS:
            break
        time.sleep(0.12)  # courtesy pacing; the weight budget is generous here

    bars = [b for b in bars if start_ms <= b["open_time"] < end_ms]
    _store(path, bars)
    return bars


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Realised funding rates over the window, oldest first.

    A perpetual charges funding every 8 hours, and this strategy holds a
    leveraged directional position for days at a time. Over a month that is
    not a rounding error, and leaving it out would be a silent subsidy to the
    backtested result - the most common way a perp backtest flatters itself."""
    path = _cache_path("funding", symbol, start_ms, end_ms)
    cached = _cached(path)
    if cached is not None:
        return cached

    api = _client()
    rates, cursor = [], start_ms
    while cursor < end_ms:
        batch = api.funding_rate(symbol=symbol, startTime=cursor, endTime=end_ms, limit=1000)
        if not batch:
            break
        for raw in batch:
            rates.append({
                "time": int(raw["fundingTime"]),
                "rate": float(raw["fundingRate"]),
            })
        cursor = int(batch[-1]["fundingTime"]) + 1
        if len(batch) < 1000:
            break
        time.sleep(0.12)

    _store(path, rates)
    return rates
