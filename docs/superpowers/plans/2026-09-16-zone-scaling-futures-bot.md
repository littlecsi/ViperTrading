# Zone-scaling Futures Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An autonomous Binance USDⓈ-M Futures bot that sizes ETHUSDT positions continuously based on price's distance to operator-defined support/resistance zones, stepping down the zone ladder on breaks.

**Architecture:** A polling loop reads market state, passes plain values to a pure decision function, and reconciles the resulting target position via market orders. All decision logic lives in one side-effect-free module (`strategy.py`) so a PPO policy can later replace it without touching the loop, execution, or logging.

**Tech Stack:** Python 3.12, `binance-futures-connector` 4.2.0, `pytest`. No async, no framework.

**Spec:** `docs/superpowers/specs/2026-09-16-zone-scaling-futures-bot-design.md`

## Global Constraints

- Target symbol is `ETHUSDT`: tick size `0.01`, lot step `0.001`, min notional `20`.
- `max_notional` is derived from **total wallet balance**, never available balance.
- Position sizes are **signed notional**: positive is long, negative is short.
- Order quantities round **down** to `stepSize`, never up.
- `strategy.py` must import nothing outside `settings` and perform no I/O — no client, no file access, no clock.
- Modules in `futures/` use flat imports (`import settings`), matching the existing `client.py` pattern; tests add `futures/` to `sys.path` via `tests/conftest.py`.
- Secrets live only in `futures/config.py`, which is gitignored and never committed.
- All commits go to the `develop` branch.

---

### Task 1: Test scaffolding

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_smoke.py`
- Modify: `requirements.txt`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing
- Produces: a working `pytest` invocation from the repo root; `futures/` importable from tests

- [ ] **Step 1: Add pytest to requirements**

Append to `requirements.txt`:

```
pytest==8.3.4
```

- [ ] **Step 2: Install it**

Run: `.viper/Scripts/pip.exe install -r requirements.txt`
Expected: pytest installs successfully.

- [ ] **Step 3: Create conftest so tests can import futures modules**

Create `tests/conftest.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "futures"))
```

- [ ] **Step 4: Write a smoke test**

Create `tests/test_smoke.py`:

```python
def test_pytest_runs():
    assert True
```

- [ ] **Step 5: Run it**

Run: `.viper/Scripts/python.exe -m pytest tests/ -v`
Expected: PASS, 1 test.

- [ ] **Step 6: Ignore the log directory**

Add to `.gitignore` under the Python section:

```
futures/logs/
```

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_smoke.py requirements.txt .gitignore
git commit -m "[add] pytest scaffolding for futures bot"
```

---

### Task 2: Settings loading and validation

**Files:**
- Create: `futures/settings.py`
- Create: `futures/settings.json`
- Create: `tests/test_settings.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Zone` frozen dataclass: `support: float`, `resistance: float` — defined here and imported by `strategy.py` to avoid a circular dependency
  - `Settings` frozen dataclass: `symbol: str`, `trend: str`, `leverage: int`, `alpha: float`, `exposure_fraction: float`, `rebalance_threshold: float`, `stop_buffer: float`, `poll_seconds: int`, `testnet: bool`, `zones: tuple[Zone, ...]`
  - `load(path: str = SETTINGS_PATH) -> Settings` — raises `ValueError` on invalid config
  - `SETTINGS_PATH: str`, `LONG: str`, `SHORT: str`

- [ ] **Step 1: Write failing tests**

Create `tests/test_settings.py`:

```python
import json
import pytest
import settings


def write(tmp_path, data):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(data))
    return str(p)


def valid_data():
    return {
        "symbol": "ETHUSDT",
        "trend": "long",
        "leverage": 5,
        "alpha": 2.0,
        "exposure_fraction": 1.0,
        "rebalance_threshold": 0.05,
        "stop_buffer": 0.01,
        "poll_seconds": 5,
        "testnet": True,
        "zones": [
            {"support": 2625.00, "resistance": 3284.04},
            {"support": 2371.26, "resistance": 2625.00},
            {"support": 1872.46, "resistance": 2371.26},
        ],
    }


def test_loads_valid_settings(tmp_path):
    s = settings.load(write(tmp_path, valid_data()))
    assert s.symbol == "ETHUSDT"
    assert s.trend == "long"
    assert s.leverage == 5
    assert len(s.zones) == 3
    assert s.zones[0].support == 2625.00
    assert s.zones[0].resistance == 3284.04


def test_rejects_unknown_trend(tmp_path):
    data = valid_data()
    data["trend"] = "sideways"
    with pytest.raises(ValueError, match="trend"):
        settings.load(write(tmp_path, data))


def test_rejects_non_positive_leverage(tmp_path):
    data = valid_data()
    data["leverage"] = 0
    with pytest.raises(ValueError, match="leverage"):
        settings.load(write(tmp_path, data))


def test_rejects_support_above_resistance(tmp_path):
    data = valid_data()
    data["zones"][0] = {"support": 3000, "resistance": 2000}
    with pytest.raises(ValueError, match="support"):
        settings.load(write(tmp_path, data))


def test_rejects_zones_not_ordered_high_to_low(tmp_path):
    data = valid_data()
    data["zones"] = [
        {"support": 1872.46, "resistance": 2371.26},
        {"support": 2625.00, "resistance": 3284.04},
    ]
    with pytest.raises(ValueError, match="descending"):
        settings.load(write(tmp_path, data))


def test_rejects_empty_zones(tmp_path):
    data = valid_data()
    data["zones"] = []
    with pytest.raises(ValueError, match="zones"):
        settings.load(write(tmp_path, data))


def test_rejects_exposure_fraction_out_of_range(tmp_path):
    data = valid_data()
    data["exposure_fraction"] = 1.5
    with pytest.raises(ValueError, match="exposure_fraction"):
        settings.load(write(tmp_path, data))
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_settings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'settings'`

- [ ] **Step 3: Write the implementation**

Create `futures/settings.py`:

```python
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
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_settings.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Create the real settings file**

Create `futures/settings.json`:

```json
{
  "symbol": "ETHUSDT",
  "trend": "long",
  "leverage": 5,
  "alpha": 2.0,
  "exposure_fraction": 1.0,
  "rebalance_threshold": 0.05,
  "stop_buffer": 0.01,
  "poll_seconds": 5,
  "testnet": true,
  "zones": [
    { "support": 2625.00, "resistance": 3284.04 },
    { "support": 2371.26, "resistance": 2625.00 },
    { "support": 1872.46, "resistance": 2371.26 }
  ]
}
```

- [ ] **Step 6: Verify the real file loads**

Run: `cd futures && ../.viper/Scripts/python.exe -c "import settings; print(settings.load())"`
Expected: prints a `Settings(...)` with 3 zones.

- [ ] **Step 7: Commit**

```bash
git add futures/settings.py futures/settings.json tests/test_settings.py
git commit -m "[add] settings loading and validation for futures bot"
```

---

### Task 3: Strategy — sizing maths

**Files:**
- Create: `futures/strategy.py`
- Create: `tests/test_strategy.py`

**Interfaces:**
- Consumes: `settings.Zone`, `settings.LONG`, `settings.SHORT`
- Produces:
  - `max_notional(wallet_balance: float, leverage: int, exposure_fraction: float) -> float`
  - `distance(price: float, zone: Zone, trend: str) -> float` — clamped to `[0, 1]`
  - `target_notional(d: float, max_n: float, alpha: float) -> float`
  - `signed(target: float, trend: str) -> float`

- [ ] **Step 1: Write failing tests**

Create `tests/test_strategy.py`:

```python
import pytest
import strategy
from settings import Zone, LONG, SHORT

ZONE = Zone(support=2371.26, resistance=2625.00)


def test_max_notional():
    assert strategy.max_notional(1000.0, 5, 1.0) == 5000.0
    assert strategy.max_notional(1000.0, 5, 0.5) == 2500.0


def test_distance_long_at_support_is_one():
    assert strategy.distance(2371.26, ZONE, LONG) == pytest.approx(1.0)


def test_distance_long_at_resistance_is_zero():
    assert strategy.distance(2625.00, ZONE, LONG) == pytest.approx(0.0)


def test_distance_long_midpoint_is_half():
    mid = (2371.26 + 2625.00) / 2
    assert strategy.distance(mid, ZONE, LONG) == pytest.approx(0.5)


def test_distance_short_is_mirrored():
    assert strategy.distance(2625.00, ZONE, SHORT) == pytest.approx(1.0)
    assert strategy.distance(2371.26, ZONE, SHORT) == pytest.approx(0.0)


def test_distance_clamps_beyond_edges():
    assert strategy.distance(9999.0, ZONE, LONG) == 0.0
    assert strategy.distance(1.0, ZONE, LONG) == 1.0
    assert strategy.distance(9999.0, ZONE, SHORT) == 1.0
    assert strategy.distance(1.0, ZONE, SHORT) == 0.0


def test_target_notional_alpha_two_curve():
    assert strategy.target_notional(0.0, 1000.0, 2.0) == pytest.approx(0.0)
    assert strategy.target_notional(0.5, 1000.0, 2.0) == pytest.approx(250.0)
    assert strategy.target_notional(1.0, 1000.0, 2.0) == pytest.approx(1000.0)


def test_target_notional_alpha_one_is_linear():
    assert strategy.target_notional(0.5, 1000.0, 1.0) == pytest.approx(500.0)


def test_signed_flips_for_short():
    assert strategy.signed(500.0, LONG) == 500.0
    assert strategy.signed(500.0, SHORT) == -500.0
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_strategy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategy'`

- [ ] **Step 3: Write the implementation**

Create `futures/strategy.py`:

```python
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
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_strategy.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add futures/strategy.py tests/test_strategy.py
git commit -m "[add] strategy sizing maths"
```

---

### Task 4: Strategy — zone selection with hysteresis

**Files:**
- Modify: `futures/strategy.py`
- Modify: `tests/test_strategy.py`

**Interfaces:**
- Consumes: `strategy.distance` from Task 3
- Produces:
  - `select_zone(price: float, zones: tuple[Zone, ...], active_index: int | None, stop_buffer: float) -> int | None`
  - `past_adverse_end(price: float, zones: tuple[Zone, ...], trend: str, stop_buffer: float) -> bool`

**Why hysteresis:** the operator's zones are contiguous — one zone's support is the next zone's resistance. Without a dead band, price crossing a shared boundary flips the target from maximum to flat instantly, so price oscillating around that boundary would repeatedly open and dump a full-size position. `active_index` is retained until price leaves the active zone by `stop_buffer`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_strategy.py`:

```python
ZONES = (
    Zone(support=2625.00, resistance=3284.04),
    Zone(support=2371.26, resistance=2625.00),
    Zone(support=1872.46, resistance=2371.26),
)


def test_select_zone_finds_containing_zone_when_none_active():
    assert strategy.select_zone(2403.0, ZONES, None, 0.01) == 1
    assert strategy.select_zone(3000.0, ZONES, None, 0.01) == 0
    assert strategy.select_zone(2000.0, ZONES, None, 0.01) == 2


def test_select_zone_returns_none_outside_ladder():
    assert strategy.select_zone(1000.0, ZONES, None, 0.01) is None
    assert strategy.select_zone(9999.0, ZONES, None, 0.01) is None


def test_select_zone_holds_active_zone_inside_buffer():
    # 2371.20 is just below zone 1's support but inside the 1% dead band
    assert strategy.select_zone(2371.20, ZONES, 1, 0.01) == 1


def test_select_zone_releases_active_zone_beyond_buffer():
    # 2371.26 * 0.99 = 2347.55; below that, zone 1 is released
    assert strategy.select_zone(2340.0, ZONES, 1, 0.01) == 2


def test_select_zone_hysteresis_is_symmetric_upward():
    # zone 2 held until price exceeds 2371.26 * 1.01 = 2394.97
    assert strategy.select_zone(2380.0, ZONES, 2, 0.01) == 2
    assert strategy.select_zone(2400.0, ZONES, 2, 0.01) == 1


def test_past_adverse_end_long_below_lowest_support():
    # lowest support 1872.46 * 0.99 = 1853.74
    assert strategy.past_adverse_end(1840.0, ZONES, LONG, 0.01) is True
    assert strategy.past_adverse_end(1860.0, ZONES, LONG, 0.01) is False


def test_past_adverse_end_short_above_highest_resistance():
    # highest resistance 3284.04 * 1.01 = 3316.88
    assert strategy.past_adverse_end(3400.0, ZONES, SHORT, 0.01) is True
    assert strategy.past_adverse_end(3300.0, ZONES, SHORT, 0.01) is False


def test_past_adverse_end_ignores_favourable_side():
    assert strategy.past_adverse_end(9999.0, ZONES, LONG, 0.01) is False
    assert strategy.past_adverse_end(1000.0, ZONES, SHORT, 0.01) is False
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_strategy.py -v`
Expected: FAIL with `AttributeError: module 'strategy' has no attribute 'select_zone'`

- [ ] **Step 3: Write the implementation**

Append to `futures/strategy.py`:

```python
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
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_strategy.py -v`
Expected: PASS, 17 tests.

- [ ] **Step 5: Commit**

```bash
git add futures/strategy.py tests/test_strategy.py
git commit -m "[add] zone selection with hysteresis dead band"
```

---

### Task 5: Strategy — the decision function

**Files:**
- Modify: `futures/strategy.py`
- Modify: `tests/test_strategy.py`

**Interfaces:**
- Consumes: everything from Tasks 3 and 4
- Produces:
  - `Decision` frozen dataclass: `action: str`, `reason: str | None`, `zone_index: int | None`, `d: float | None`, `max_n: float`, `target_signed: float`, `delta: float`
  - `decide(price, zones, trend, position_notional, wallet_balance, leverage, alpha, exposure_fraction, stop_buffer, rebalance_threshold, min_notional, active_index) -> Decision`
  - Action constants: `HOLD`, `BUY`, `SELL`, `HALT`, `IDLE`
  - Reason constants: `SCALE_IN`, `SCALE_OUT`, `STOP_OUT`, `HALT_FLATTEN`

This is the PPO seam: plain values in, target position out.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_strategy.py`:

```python
def decide(**kw):
    base = dict(
        price=2403.0,
        zones=ZONES,
        trend=LONG,
        position_notional=0.0,
        wallet_balance=1000.0,
        leverage=5,
        alpha=2.0,
        exposure_fraction=1.0,
        stop_buffer=0.01,
        rebalance_threshold=0.05,
        min_notional=20.0,
        active_index=None,
    )
    base.update(kw)
    return strategy.decide(**base)


def test_decide_opens_position_from_flat():
    d = decide()
    assert d.action == strategy.BUY
    assert d.reason == strategy.SCALE_IN
    assert d.zone_index == 1
    assert d.delta > 0
    assert d.target_signed == pytest.approx(d.delta)


def test_decide_holds_when_delta_below_threshold():
    first = decide()
    d = decide(position_notional=first.target_signed)
    assert d.action == strategy.HOLD
    assert d.delta == 0.0


def test_decide_scales_out_when_price_rises_toward_resistance():
    held = decide().target_signed
    d = decide(price=2550.0, position_notional=held, active_index=1)
    assert d.action == strategy.SELL
    assert d.reason == strategy.SCALE_OUT
    assert d.delta < 0


def test_decide_flat_target_at_resistance():
    d = decide(price=2625.0, position_notional=0.0, active_index=1)
    assert d.target_signed == pytest.approx(0.0)


def test_decide_max_target_at_support():
    d = decide(price=2371.26, position_notional=0.0, active_index=1)
    assert d.target_signed == pytest.approx(5000.0)


def test_decide_stop_out_on_zone_change_while_holding():
    d = decide(price=2340.0, position_notional=5000.0, active_index=1)
    assert d.zone_index == 2
    assert d.reason == strategy.STOP_OUT
    assert d.action == strategy.SELL
    assert d.delta < 0


def test_decide_halts_below_ladder():
    d = decide(price=1800.0, position_notional=3000.0, active_index=2)
    assert d.action == strategy.HALT
    assert d.reason == strategy.HALT_FLATTEN
    assert d.target_signed == 0.0
    assert d.delta == pytest.approx(-3000.0)


def test_decide_idles_above_ladder_when_flat():
    d = decide(price=9999.0, position_notional=0.0)
    assert d.action == strategy.IDLE
    assert d.delta == 0.0


def test_decide_short_trend_targets_negative_notional():
    d = decide(trend=SHORT, price=2600.0, active_index=1)
    assert d.target_signed < 0
    assert d.action == strategy.SELL


def test_decide_respects_min_notional_over_threshold():
    # tiny max_notional makes the 5% threshold smaller than min_notional
    d = decide(wallet_balance=10.0, price=2624.0, active_index=1)
    assert d.action == strategy.HOLD
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_strategy.py -v`
Expected: FAIL with `AttributeError: module 'strategy' has no attribute 'decide'`

- [ ] **Step 3: Write the implementation**

Change the import line at the top of `futures/strategy.py` to add the dataclass import:

```python
from dataclasses import dataclass

from settings import Zone, LONG, SHORT
```

Then append to `futures/strategy.py`:

```python
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
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_strategy.py -v`
Expected: PASS, 27 tests.

- [ ] **Step 5: Commit**

```bash
git add futures/strategy.py tests/test_strategy.py
git commit -m "[add] pure decision function for zone-scaling strategy"
```

---

### Task 6: Client — testnet support

**Files:**
- Modify: `futures/client.py`
- Delete: `futures/trading.py`

**Interfaces:**
- Consumes: `config.api_key`, `config.api_secret`, `config.testnet_key`, `config.testnet_secret`
- Produces: `build(testnet: bool) -> UMFutures`, `sync_time(testnet: bool) -> int`, `test_connection(testnet: bool = True) -> None`

**Clock skew is mandatory, not optional.** This has been measured empirically on
the target machine: the local clock runs ~2000ms **ahead** of Binance's server,
and Binance rejects any signed request with a future timestamp (error -1021,
"Timestamp for this request was 1000ms ahead of the server's time"). The
connector builds timestamps from raw `time.time()` and exposes no offset hook,
so without this fix **every authenticated call fails** and nothing past Task 6
can work.

The patch target is subtle and already cost investigation time: patch
`binance.api.get_timestamp`, **not** `binance.lib.utils.get_timestamp`.
`api.py` does `from binance.lib.utils import get_timestamp`, binding the name
into its own module namespace, so rebinding it in `lib.utils` has no effect on
the code that actually runs.

`trading.py` is superseded — its balance lookup moves to `market.py` (Task 7) and its order placement to `execution.py` (Task 8). Removing it now prevents a second, divergent path to the same endpoints.

- [ ] **Step 1: Add testnet credentials to config**

Add to `futures/config.py` (gitignored — the operator supplies real values):

```python
testnet_key = ""
testnet_secret = ""
```

- [ ] **Step 2: Rewrite the client module**

Replace the contents of `futures/client.py`:

```python
import time

import binance.api
from binance.um_futures import UMFutures

import config

TESTNET_URL = "https://testnet.binancefuture.com"


def _measure_offset(probe: UMFutures, samples: int = 5) -> int:
    """Median of (server time - local midpoint) in milliseconds.

    Uses the midpoint of the local clock readings taken either side of the
    call so that network latency cancels out instead of biasing the offset."""
    offsets = []
    for _ in range(samples):
        before = int(time.time() * 1000)
        server = probe.time()["serverTime"]
        after = int(time.time() * 1000)
        offsets.append(server - (before + after) // 2)
    return sorted(offsets)[len(offsets) // 2]


def sync_time(testnet: bool) -> int:
    """Align outgoing request timestamps with Binance's clock.

    Binance rejects signed requests whose timestamp runs ahead of its server,
    and this machine's clock does exactly that. The connector offers no offset
    hook, so the offset is measured once against a public endpoint and applied
    to every timestamp thereafter.

    Patches binance.api rather than binance.lib.utils: api.py imports
    get_timestamp by name, so rebinding it in the source module would not
    affect the code that actually runs."""
    probe = UMFutures(base_url=TESTNET_URL) if testnet else UMFutures()
    offset = _measure_offset(probe)
    binance.api.get_timestamp = lambda: int(time.time() * 1000) + offset
    return offset


def build(testnet: bool) -> UMFutures:
    """Construct a Futures client. Testnet uses entirely separate
    credentials from live — they are not interchangeable."""
    sync_time(testnet)
    if testnet:
        return UMFutures(
            key=config.testnet_key,
            secret=config.testnet_secret,
            base_url=TESTNET_URL,
        )
    return UMFutures(key=config.api_key, secret=config.api_secret)


def test_connection(testnet: bool = True) -> None:
    client = build(testnet)
    client.ping()
    balances = client.balance()
    usdt = next((b for b in balances if b["asset"] == "USDT"), None)
    label = "testnet" if testnet else "live"
    print(f"Connected to Binance Futures ({label}).")
    print("USDT balance:", usdt["balance"] if usdt else "N/A")


if __name__ == "__main__":
    test_connection()
```

- [ ] **Step 3: Remove the superseded module**

```bash
git rm futures/trading.py
```

- [ ] **Step 4: Verify the testnet connection**

Run: `cd futures && ../.viper/Scripts/python.exe client.py`
Expected: prints the testnet USDT balance (~1000). If it raises a signature error or `-2015`, the testnet keys in `config.py` are missing or wrong — stop and tell the operator rather than falling back to live.

- [ ] **Step 5: Commit**

```bash
git add futures/client.py
git commit -m "[add] testnet support in futures client, drop superseded trading module"
```

---

### Task 7: Market reads

**Files:**
- Create: `futures/market.py`
- Create: `tests/test_market.py`

**Interfaces:**
- Consumes: a `UMFutures` client from `client.build`
- Produces:
  - `Filters` frozen dataclass: `step_size: float`, `min_qty: float`, `min_notional: float`
  - `get_filters(client, symbol: str) -> Filters`
  - `get_mark_price(client, symbol: str) -> float`
  - `get_wallet_balance(client, asset: str = "USDT") -> float`
  - `get_available_balance(client, asset: str = "USDT") -> float`
  - `get_position_amt(client, symbol: str) -> float` — signed; positive long, negative short
  - `get_unrealized_pnl(client, symbol: str) -> float`
  - `set_leverage(client, symbol: str, leverage: int) -> None`

Functions take the client as their first argument and hold no module-level state, matching the existing `biat.py` convention.

- [ ] **Step 1: Write failing tests**

Create `tests/test_market.py`. These use a hand-written fake rather than a mocking library — the fake documents the exact API response shapes the code depends on:

```python
import market


class FakeClient:
    def __init__(self):
        self.leverage_calls = []

    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "ETHUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "20"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    ],
                }
            ]
        }

    def ticker_price(self, symbol):
        return {"symbol": symbol, "price": "2403.00"}

    def balance(self):
        return [
            {"asset": "USDT", "balance": "1000.0", "availableBalance": "850.5"},
            {"asset": "BNB", "balance": "0.0", "availableBalance": "0.0"},
        ]

    def get_position_risk(self, symbol=None):
        return [{"symbol": "ETHUSDT", "positionAmt": "-1.250", "unRealizedProfit": "-12.5"}]

    def change_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))
        return {"leverage": leverage, "symbol": symbol}


def test_get_filters():
    f = market.get_filters(FakeClient(), "ETHUSDT")
    assert f.step_size == 0.001
    assert f.min_qty == 0.001
    assert f.min_notional == 20.0


def test_get_mark_price():
    assert market.get_mark_price(FakeClient(), "ETHUSDT") == 2403.00


def test_get_wallet_balance_uses_total_not_available():
    assert market.get_wallet_balance(FakeClient()) == 1000.0


def test_get_available_balance():
    assert market.get_available_balance(FakeClient()) == 850.5


def test_get_balance_unknown_asset_returns_zero():
    assert market.get_wallet_balance(FakeClient(), "DOGE") == 0.0


def test_get_position_amt_preserves_sign():
    assert market.get_position_amt(FakeClient(), "ETHUSDT") == -1.250


def test_get_position_amt_unknown_symbol_is_zero():
    assert market.get_position_amt(FakeClient(), "BTCUSDT") == 0.0


def test_get_unrealized_pnl():
    assert market.get_unrealized_pnl(FakeClient(), "ETHUSDT") == -12.5


def test_set_leverage_calls_client():
    c = FakeClient()
    market.set_leverage(c, "ETHUSDT", 5)
    assert c.leverage_calls == [("ETHUSDT", 5)]
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_market.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'market'`

- [ ] **Step 3: Write the implementation**

Create `futures/market.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Filters:
    step_size: float
    min_qty: float
    min_notional: float


def get_filters(client, symbol: str) -> Filters:
    info = client.exchange_info()
    entry = next(s for s in info["symbols"] if s["symbol"] == symbol)

    step_size = min_qty = min_notional = 0.0
    for f in entry["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
            min_qty = float(f["minQty"])
        elif f["filterType"] == "MIN_NOTIONAL":
            min_notional = float(f["notional"])

    return Filters(step_size=step_size, min_qty=min_qty, min_notional=min_notional)


def get_mark_price(client, symbol: str) -> float:
    return float(client.ticker_price(symbol)["price"])


def _balance_entry(client, asset: str):
    return next((b for b in client.balance() if b["asset"] == asset), None)


def get_wallet_balance(client, asset: str = "USDT") -> float:
    """Total wallet balance. Position sizing must use this rather than
    available balance, which shrinks as margin is consumed."""
    entry = _balance_entry(client, asset)
    return float(entry["balance"]) if entry else 0.0


def get_available_balance(client, asset: str = "USDT") -> float:
    entry = _balance_entry(client, asset)
    return float(entry["availableBalance"]) if entry else 0.0


def _position_entry(client, symbol: str):
    positions = client.get_position_risk(symbol=symbol)
    return next((p for p in positions if p["symbol"] == symbol), None)


def get_position_amt(client, symbol: str) -> float:
    """Signed position size in contracts: positive long, negative short."""
    entry = _position_entry(client, symbol)
    return float(entry["positionAmt"]) if entry else 0.0


def get_unrealized_pnl(client, symbol: str) -> float:
    entry = _position_entry(client, symbol)
    return float(entry["unRealizedProfit"]) if entry else 0.0


def set_leverage(client, symbol: str, leverage: int) -> None:
    client.change_leverage(symbol=symbol, leverage=leverage)
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_market.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Verify against the real testnet API**

Run:

```bash
cd futures && ../.viper/Scripts/python.exe -c "
import client, market
c = client.build(True)
print('filters:', market.get_filters(c, 'ETHUSDT'))
print('price:', market.get_mark_price(c, 'ETHUSDT'))
print('wallet:', market.get_wallet_balance(c))
print('position:', market.get_position_amt(c, 'ETHUSDT'))
"
```

Expected: real values. This confirms the fake's response shapes match the live API — if a `KeyError` appears here, the fake is wrong, not the API.

- [ ] **Step 6: Commit**

```bash
git add futures/market.py tests/test_market.py
git commit -m "[add] market read helpers for futures bot"
```

---

### Task 8: Execution

**Files:**
- Create: `futures/execution.py`
- Create: `tests/test_execution.py`

**Interfaces:**
- Consumes: `market.Filters`
- Produces:
  - `quantity_for(delta_notional: float, price: float, filters: Filters) -> float` — absolute quantity, floored to step
  - `is_executable(qty: float, price: float, filters: Filters) -> bool`
  - `execute(client, symbol: str, side: str, qty: float) -> dict`

- [ ] **Step 1: Write failing tests**

Create `tests/test_execution.py`:

```python
import execution
from market import Filters

F = Filters(step_size=0.001, min_qty=0.001, min_notional=20.0)


def test_quantity_floors_to_step_size():
    # 100 / 2403 = 0.041615... -> 0.041
    assert execution.quantity_for(100.0, 2403.0, F) == 0.041


def test_quantity_is_absolute_for_negative_delta():
    assert execution.quantity_for(-100.0, 2403.0, F) == 0.041


def test_quantity_never_rounds_up():
    qty = execution.quantity_for(0.0409999 * 2403.0, 2403.0, F)
    assert qty <= 0.041


def test_quantity_below_step_is_zero():
    assert execution.quantity_for(1.0, 2403.0, F) == 0.0


def test_is_executable_rejects_below_min_qty():
    assert execution.is_executable(0.0, 2403.0, F) is False


def test_is_executable_rejects_below_min_notional():
    # 0.005 * 2403 = 12.0, under the 20 minimum
    assert execution.is_executable(0.005, 2403.0, F) is False


def test_is_executable_accepts_valid_order():
    # 0.041 * 2403 = 98.5
    assert execution.is_executable(0.041, 2403.0, F) is True


def test_execute_sends_market_order():
    class FakeClient:
        def __init__(self):
            self.orders = []

        def new_order(self, **kwargs):
            self.orders.append(kwargs)
            return {"orderId": 1, "status": "FILLED", **kwargs}

    c = FakeClient()
    result = execution.execute(c, "ETHUSDT", "BUY", 0.041)
    assert c.orders == [
        {"symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.041}
    ]
    assert result["orderId"] == 1
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_execution.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'execution'`

- [ ] **Step 3: Write the implementation**

Create `futures/execution.py`:

```python
import math

from market import Filters


def quantity_for(delta_notional: float, price: float, filters: Filters) -> float:
    """Absolute order quantity for a notional delta, floored to the lot step.

    Always rounds DOWN so the bot cannot overshoot its own exposure cap."""
    raw = abs(delta_notional) / price
    steps = math.floor(raw / filters.step_size)
    qty = steps * filters.step_size
    # floor() on binary floats leaves trailing noise; the lot step has at most
    # 8 decimals, so rounding there is exact without reintroducing overshoot.
    return round(qty, 8)


def is_executable(qty: float, price: float, filters: Filters) -> bool:
    if qty <= 0 or qty < filters.min_qty:
        return False
    return qty * price >= filters.min_notional


def execute(client, symbol: str, side: str, qty: float) -> dict:
    """Send a market order. Raises on failure — a rejected order must surface
    rather than be silently swallowed."""
    return client.new_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_execution.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add futures/execution.py tests/test_execution.py
git commit -m "[add] order execution with lot-step flooring and filter checks"
```

---

### Task 9: Journal

**Files:**
- Create: `futures/journal.py`
- Create: `tests/test_journal.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `log_tick(record: dict, log_dir: str | None = None) -> None` — appends to `ticks-YYYY-MM-DD.jsonl`
  - `log_order(record: dict, log_dir: str | None = None) -> None` — appends to `orders-YYYY-MM-DD.jsonl`
  - `LOG_DIR: str`

The order record is deliberately denormalised: it carries the full environment snapshot at fill time so a trade's context never has to be reconstructed by joining against the tick log.

- [ ] **Step 1: Write failing tests**

Create `tests/test_journal.py`:

```python
import json
from pathlib import Path

import journal


def test_log_tick_appends_jsonl(tmp_path):
    journal.log_tick({"price": 2403.0, "action": "buy"}, log_dir=str(tmp_path))
    journal.log_tick({"price": 2404.0, "action": "hold"}, log_dir=str(tmp_path))

    files = list(Path(tmp_path).glob("ticks-*.jsonl"))
    assert len(files) == 1

    lines = files[0].read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["price"] == 2403.0
    assert json.loads(lines[1])["action"] == "hold"


def test_log_tick_adds_timestamp(tmp_path):
    journal.log_tick({"action": "hold"}, log_dir=str(tmp_path))
    record = json.loads(list(Path(tmp_path).glob("ticks-*.jsonl"))[0].read_text())
    assert "timestamp" in record


def test_log_order_writes_separate_file(tmp_path):
    journal.log_order({"side": "BUY", "support": 2371.26}, log_dir=str(tmp_path))

    orders = list(Path(tmp_path).glob("orders-*.jsonl"))
    assert len(orders) == 1
    assert list(Path(tmp_path).glob("ticks-*.jsonl")) == []

    record = json.loads(orders[0].read_text())
    assert record["side"] == "BUY"
    assert record["support"] == 2371.26


def test_creates_log_dir_if_missing(tmp_path):
    target = tmp_path / "nested" / "logs"
    journal.log_tick({"action": "hold"}, log_dir=str(target))
    assert target.exists()
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_journal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'journal'`

- [ ] **Step 3: Write the implementation**

Create `futures/journal.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = str(Path(__file__).resolve().parent / "logs")


def _append(prefix: str, record: dict, log_dir: str | None) -> None:
    directory = Path(log_dir or LOG_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    record = {"timestamp": now.isoformat(), **record}

    path = directory / f"{prefix}-{now.date().isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def log_tick(record: dict, log_dir: str | None = None) -> None:
    """One line per loop iteration, including no-ops. This is the continuous
    state/action history a PPO policy trains against."""
    _append("ticks", record, log_dir)


def log_order(record: dict, log_dir: str | None = None) -> None:
    """One line per executed order, carrying the full environment snapshot at
    fill time so a trade's context is never reconstructed by joining logs."""
    _append("orders", record, log_dir)
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_journal.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add futures/journal.py tests/test_journal.py
git commit -m "[add] tick and order JSONL journals"
```

---

### Task 10: The bot loop

**Files:**
- Create: `futures/bot.py`

**Interfaces:**
- Consumes: `settings.load`, `client.build`, `market.*`, `strategy.decide`, `execution.*`, `journal.*`
- Produces: `run() -> None`; runnable as `python futures/bot.py`

The loop holds the only mutable state: `active_index` (the current zone) and `halted`. Everything else is re-read each tick.

- [ ] **Step 1: Write the loop**

Create `futures/bot.py`:

```python
import time
import traceback

import client
import execution
import journal
import market
import settings
import strategy


def run() -> None:
    cfg = settings.load()
    api = client.build(cfg.testnet)

    market.set_leverage(api, cfg.symbol, cfg.leverage)
    filters = market.get_filters(api, cfg.symbol)

    label = "TESTNET" if cfg.testnet else "LIVE"
    print(f"Viper starting on {label} — {cfg.symbol} @ {cfg.leverage}x, trend={cfg.trend}")

    active_index = None
    halted = False

    while True:
        try:
            # Re-read settings each tick so trend, leverage, and zones can be
            # changed without a restart. Invalid edits are ignored, not fatal.
            try:
                new_cfg = settings.load()
                if new_cfg != cfg:
                    if new_cfg.leverage != cfg.leverage:
                        market.set_leverage(api, new_cfg.symbol, new_cfg.leverage)
                    cfg = new_cfg
                    halted = False
                    print("settings reloaded")
            except (ValueError, KeyError, OSError) as exc:
                journal.log_tick({"action": "config_error", "error": str(exc)})

            price = market.get_mark_price(api, cfg.symbol)
            position_amt = market.get_position_amt(api, cfg.symbol)
            position_notional = position_amt * price
            wallet = market.get_wallet_balance(api)
            available = market.get_available_balance(api)
            pnl = market.get_unrealized_pnl(api, cfg.symbol)

            decision = strategy.decide(
                price=price,
                zones=cfg.zones,
                trend=cfg.trend,
                position_notional=position_notional,
                wallet_balance=wallet,
                leverage=cfg.leverage,
                alpha=cfg.alpha,
                exposure_fraction=cfg.exposure_fraction,
                stop_buffer=cfg.stop_buffer,
                rebalance_threshold=cfg.rebalance_threshold,
                min_notional=filters.min_notional,
                active_index=active_index,
            )

            zone = cfg.zones[decision.zone_index] if decision.zone_index is not None else None

            journal.log_tick(
                {
                    "symbol": cfg.symbol,
                    "mark_price": price,
                    "trend": cfg.trend,
                    "leverage": cfg.leverage,
                    "active_zone_index": decision.zone_index,
                    "support": zone.support if zone else None,
                    "resistance": zone.resistance if zone else None,
                    "d": decision.d,
                    "max_notional": decision.max_n,
                    "target_notional": decision.target_signed,
                    "current_notional": position_notional,
                    "delta": decision.delta,
                    "action": decision.action,
                    "reason": decision.reason,
                    "balance": wallet,
                    "unrealized_pnl": pnl,
                }
            )

            if decision.action in (strategy.HOLD, strategy.IDLE):
                active_index = decision.zone_index
                time.sleep(cfg.poll_seconds)
                continue

            if decision.action == strategy.HALT and not halted:
                print(f"HALT — price {price} left the zone ladder")
                halted = True

            qty = execution.quantity_for(decision.delta, price, filters)
            if not execution.is_executable(qty, price, filters):
                active_index = decision.zone_index
                time.sleep(cfg.poll_seconds)
                continue

            side = "BUY" if decision.delta > 0 else "SELL"
            result = execution.execute(api, cfg.symbol, side, qty)

            position_after = market.get_position_amt(api, cfg.symbol)

            journal.log_order(
                {
                    "order_id": result.get("orderId"),
                    "client_order_id": result.get("clientOrderId"),
                    "symbol": cfg.symbol,
                    "side": side,
                    "reason": decision.reason,
                    "quantity": qty,
                    "notional": qty * price,
                    "mark_price": price,
                    "trend": cfg.trend,
                    "leverage": cfg.leverage,
                    "alpha": cfg.alpha,
                    "exposure_fraction": cfg.exposure_fraction,
                    "active_zone_index": decision.zone_index,
                    "support": zone.support if zone else None,
                    "resistance": zone.resistance if zone else None,
                    "d": decision.d,
                    "max_notional": decision.max_n,
                    "target_notional": decision.target_signed,
                    "position_before": position_amt,
                    "position_after": position_after,
                    "balance": wallet,
                    "available_balance": available,
                    "unrealized_pnl": pnl,
                }
            )

            print(f"{side} {qty} {cfg.symbol} @ ~{price} ({decision.reason})")
            active_index = decision.zone_index

        except KeyboardInterrupt:
            print("stopped by operator")
            return
        except Exception as exc:
            # A transient API error must not kill an unattended bot, but it
            # must be recorded rather than swallowed.
            journal.log_tick({"action": "error", "error": str(exc)})
            print("ERROR:", exc)
            traceback.print_exc()

        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Verify the full test suite still passes**

Run: `.viper/Scripts/python.exe -m pytest tests/ -v`
Expected: PASS, 56 tests (1 smoke + 7 settings + 27 strategy + 9 market + 8 execution + 4 journal).

- [ ] **Step 3: Dry-check the loop against testnet**

Temporarily raise `rebalance_threshold` so the bot decides but never trades:

```bash
cd futures && ../.viper/Scripts/python.exe -c "
import json, pathlib
p = pathlib.Path('settings.json')
data = json.loads(p.read_text())
data['rebalance_threshold'] = 999
p.write_text(json.dumps(data, indent=2))
print('threshold raised — bot will decide but not trade')
"
```

Then run `../.viper/Scripts/python.exe bot.py` for ~30 seconds, confirm it prints the startup banner and writes tick records, and stop it with Ctrl+C.

Inspect the last few records in `futures/logs/ticks-*.jsonl`.
Expected: records with populated `d`, `target_notional`, and `action: "hold"`.

- [ ] **Step 4: Restore the threshold**

```bash
cd futures && ../.viper/Scripts/python.exe -c "
import json, pathlib
p = pathlib.Path('settings.json')
data = json.loads(p.read_text())
data['rebalance_threshold'] = 0.05
p.write_text(json.dumps(data, indent=2))
print('threshold restored')
"
```

- [ ] **Step 5: Commit**

```bash
git add futures/bot.py
git commit -m "[add] autonomous trading loop"
```

---

### Task 11: Documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `HANDOFF.md`

**Interfaces:**
- Consumes: the finished implementation
- Produces: accurate project docs

`CLAUDE.md` currently describes only the legacy Spot bot and is now substantially wrong about what this repo contains.

- [ ] **Step 1: Rewrite the CLAUDE.md project overview**

Replace the "Project overview" and "Running" sections so they describe the Futures bot in `futures/` as the active system, note that `binance/` is legacy Spot code retained but unused, and document `python futures/bot.py` as the entry point. Record the architecture rules that constrain future edits:

- `strategy.py` is pure — no I/O, no client, no clock
- sizing uses total wallet balance, never available balance
- quantities floor to lot step, never round up
- `settings.json` is re-read every tick; invalid edits are ignored, not fatal
- zones are contiguous, so `stop_buffer` doubles as the hysteresis dead band

- [ ] **Step 2: Update HANDOFF.md**

Replace the "Where I left off" section with the current state: the bot is implemented and testnet-validated, `trading.py` is gone, and the next step is extended testnet observation before any live run. Update the setup steps to note that `config.py` now needs four values (`api_key`, `api_secret`, `testnet_key`, `testnet_secret`) and that `pip install -r requirements.txt` now also brings in pytest.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md HANDOFF.md
git commit -m "[docs] update project docs for futures bot"
```

---

## Verification

After all tasks:

- [ ] `.viper/Scripts/python.exe -m pytest tests/ -v` — all tests pass
- [ ] `cd futures && ../.viper/Scripts/python.exe client.py` — testnet connection confirmed
- [ ] `git status` — no stray files; `config.py`, `futures/logs/`, and `.viper` all untracked
- [ ] `git log --oneline` — one commit per task
- [ ] Bot runs against testnet for an extended period without unhandled exceptions
- [ ] `futures/logs/orders-*.jsonl` contains a complete environment snapshot per fill
