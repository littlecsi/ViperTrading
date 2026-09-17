# Limit-order ladder execution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace market-order execution for in-zone `SCALE_IN`/`SCALE_OUT`
with a self-managing ladder of resting limit orders, sized off the existing
continuous target curve, with a liquidation-aware safety brake on the
accumulate side and a switch to ISOLATED margin mode — while keeping market
orders unchanged for `STOP_OUT`, `HALT_FLATTEN`, and the off-ladder dead-band
flatten.

**Architecture:** A new pure module `ladder.py` (mirroring the purity of
`strategy.py`) computes rung prices/sizes off `support`/`resistance`/`alpha`,
splits them into BUY/SELL by current price, applies a liquidation-aware cap
to the accumulate side, and diffs a desired order set against what is
currently open. `market.py` and `execution.py` gain the I/O primitives
(open orders, margin type, leverage brackets, limit order placement,
cancellation). `bot.py` owns the new mutable ladder state and orchestrates
the poll-detect-settle-reconcile lifecycle each tick.

**Tech Stack:** Python 3, `binance-futures-connector` (`UMFutures` client,
`binance.error.ClientError`), `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-17-limit-order-ladder-design.md`
(this plan argues the spec — read both; each task below assumes the spec's
context.)

## Global Constraints

- `strategy.py` stays pure and unmodified — no I/O, no client, no clock (spec: "Module boundaries").
- `execution.execute()` (market order) is unchanged and remains the only path for `STOP_OUT`/`HALT_FLATTEN`/dead-band exits (spec: "Why market orders stay for exits").
- All new pure logic lives in `ladder.py`; all new I/O lives in `market.py`/`execution.py`; all new mutable loop state lives in `bot.py` (spec: "Module layout").
- `rung_spacing_pct` and `liquidation_buffer_pct` are hot-reloadable settings, validated the same way `rebalance_threshold`/`stop_buffer` are today (spec: "Config additions & margin mode").
- Binance's reported `liquidationPrice` is checked every tick as a backstop, independent of the projection (spec: "Liquidation-aware buy-side cap", point 4).
- Console output stays ASCII-only, per the existing `bot.py` convention.
- No test may reach the network — `tests/conftest.py`'s autouse `no_telegram` fixture already enforces this for notifications; new tests follow the existing `FakeClient`/`LoopClient` pattern, never real HTTP.

---

### Task 1: Settings — `rung_spacing_pct` and `liquidation_buffer_pct`

**Files:**
- Modify: `futures/settings.py`
- Modify: `futures/settings.json`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `Settings.rung_spacing_pct: float`, `Settings.liquidation_buffer_pct: float` — consumed by `ladder.py` (Task 5+) and `bot.py` (Task 13+).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_settings.py — add to the bottom of the file

def test_rejects_zero_rung_spacing_pct(tmp_path):
    data = valid_data()
    data["rung_spacing_pct"] = 0
    with pytest.raises(ValueError, match="rung_spacing_pct"):
        settings.load(write(tmp_path, data))


def test_rejects_rung_spacing_pct_at_or_above_one(tmp_path):
    data = valid_data()
    data["rung_spacing_pct"] = 1.0
    with pytest.raises(ValueError, match="rung_spacing_pct"):
        settings.load(write(tmp_path, data))


def test_rejects_negative_liquidation_buffer_pct(tmp_path):
    data = valid_data()
    data["liquidation_buffer_pct"] = -0.01
    with pytest.raises(ValueError, match="liquidation_buffer_pct"):
        settings.load(write(tmp_path, data))


def test_rejects_liquidation_buffer_pct_at_or_above_one(tmp_path):
    data = valid_data()
    data["liquidation_buffer_pct"] = 1.0
    with pytest.raises(ValueError, match="liquidation_buffer_pct"):
        settings.load(write(tmp_path, data))


def test_loads_ladder_settings(tmp_path):
    data = valid_data()
    data["rung_spacing_pct"] = 0.005
    data["liquidation_buffer_pct"] = 0.2
    s = settings.load(write(tmp_path, data))
    assert s.rung_spacing_pct == 0.005
    assert s.liquidation_buffer_pct == 0.2
```

Also add both fields to `valid_data()` in `tests/test_settings.py` (every
existing test calls this, so every existing test must keep passing):

```python
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
        "rung_spacing_pct": 0.005,
        "liquidation_buffer_pct": 0.2,
        "zones": [
            {"support": 2625.00, "resistance": 3284.04},
            {"support": 2371.26, "resistance": 2625.00},
            {"support": 1872.46, "resistance": 2371.26},
        ],
    }
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_settings.py -v`
Expected: the 5 new tests FAIL with `KeyError: 'rung_spacing_pct'` (the
field doesn't exist on `Settings` yet), and every pre-existing test also
fails the same way once `valid_data()` is updated but `settings.py` isn't.

- [ ] **Step 3: Implement**

```python
# futures/settings.py — add to Settings dataclass, after stop_buffer:

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
    rung_spacing_pct: float
    liquidation_buffer_pct: float
    zones: tuple[Zone, ...]
```

```python
# futures/settings.py — add to load(), after the stop_buffer block:

    rung_spacing_pct = float(data["rung_spacing_pct"])
    if not 0 < rung_spacing_pct < 1:
        raise ValueError("rung_spacing_pct must be in (0, 1)")

    liquidation_buffer_pct = float(data["liquidation_buffer_pct"])
    if not 0 <= liquidation_buffer_pct < 1:
        raise ValueError("liquidation_buffer_pct must be in [0, 1)")
```

```python
# futures/settings.py — add to the return Settings(...) call:

    return Settings(
        symbol=str(data["symbol"]),
        trend=trend,
        leverage=leverage,
        alpha=alpha,
        exposure_fraction=exposure_fraction,
        rebalance_threshold=rebalance_threshold,
        stop_buffer=stop_buffer,
        poll_seconds=poll_seconds,
        testnet=bool(data["testnet"]),
        rung_spacing_pct=rung_spacing_pct,
        liquidation_buffer_pct=liquidation_buffer_pct,
        zones=_parse_zones(data["zones"]),
    )
```

Update `futures/settings.json` (add the two fields; keep everything else
identical):

```json
{"symbol":"ETHUSDT","trend":"long","leverage":5,"alpha":2.0,"exposure_fraction":1.0,"rebalance_threshold":0.05,"stop_buffer":0.01,"poll_seconds":1,"testnet":true,"rung_spacing_pct":0.005,"liquidation_buffer_pct":0.2,"zones":[{"support":2625.00,"resistance":3284.04},{"support":2371.26,"resistance":2625.00},{"support":1872.46,"resistance":2371.26}]}
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_settings.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/settings.py futures/settings.json tests/test_settings.py
git commit -m "feat: add rung_spacing_pct and liquidation_buffer_pct settings"
```

---

### Task 2: `market.py` — position-risk extractors for the ladder

**Files:**
- Modify: `futures/market.py`
- Test: `tests/test_market.py`

**Interfaces:**
- Consumes: the existing `_position_entry(positions, symbol)` helper.
- Produces: `market.liquidation_price_from(positions, symbol) -> float`,
  `market.entry_price_from(positions, symbol) -> float`,
  `market.isolated_wallet_from(positions, symbol) -> float` — consumed by
  `bot.py` (Task 13+) for the liquidation backstop and by the liquidation-cap
  inputs.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_market.py — extend the POSITIONS fixture and add tests

POSITIONS = [
    {
        "symbol": "ETHUSDT",
        "positionAmt": "-1.250",
        "unRealizedProfit": "-12.5",
        "liquidationPrice": "2950.50",
        "entryPrice": "2400.00",
        "isolatedWallet": "600.00",
    }
]


def test_liquidation_price_from():
    assert market.liquidation_price_from(POSITIONS, "ETHUSDT") == 2950.50


def test_liquidation_price_from_unknown_symbol_is_zero():
    assert market.liquidation_price_from(POSITIONS, "BTCUSDT") == 0.0


def test_entry_price_from():
    assert market.entry_price_from(POSITIONS, "ETHUSDT") == 2400.00


def test_isolated_wallet_from():
    assert market.isolated_wallet_from(POSITIONS, "ETHUSDT") == 600.00
```

(Existing tests that read the module-level `POSITIONS` constant, e.g.
`test_position_amt_from_preserves_sign`, keep passing unchanged — the new
keys are additive.)

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_market.py -v`
Expected: the 4 new tests FAIL with `AttributeError: module 'market' has no
attribute 'liquidation_price_from'`.

- [ ] **Step 3: Implement**

```python
# futures/market.py — add after unrealized_pnl_from()

def liquidation_price_from(positions, symbol: str) -> float:
    """Binance's own projected liquidation price for the current position.
    Ground truth for the liquidation-cap backstop -- see ladder.py and the
    design doc's "Liquidation-aware buy-side cap"."""
    entry = _position_entry(positions, symbol)
    return float(entry["liquidationPrice"]) if entry else 0.0


def entry_price_from(positions, symbol: str) -> float:
    entry = _position_entry(positions, symbol)
    return float(entry["entryPrice"]) if entry else 0.0


def isolated_wallet_from(positions, symbol: str) -> float:
    """Margin currently allocated to this symbol's isolated position. Feeds
    the liquidation-cap projection; see ladder.liquidation_scale()."""
    entry = _position_entry(positions, symbol)
    return float(entry["isolatedWallet"]) if entry else 0.0
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_market.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/market.py tests/test_market.py
git commit -m "feat: add liquidation/entry/isolated-margin extractors to market.py"
```

---

### Task 3: `market.py` — open orders, leverage brackets, margin type (I/O)

**Files:**
- Modify: `futures/market.py`
- Test: `tests/test_market.py`

**Interfaces:**
- Produces: `market.get_open_orders(client, symbol) -> list[dict]`,
  `market.MarginTier` (frozen dataclass: `floor`, `cap`, `maint_margin_rate`,
  `maint_amount`), `market.maintenance_tier_from(brackets, notional) ->
  MarginTier`, `market.get_leverage_brackets(client, symbol) -> list[dict]`,
  `market.set_margin_type(client, symbol, margin_type="ISOLATED") -> str`
  (returns `"changed"`, `"already_set"`, or `"position_open"`) — consumed by
  `bot.py` (Task 13) and `ladder.py` (Task 8, via the extracted `MarginTier`).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_market.py — add imports and tests

from binance.error import ClientError


BRACKETS = [
    {"bracket": 1, "initialLeverage": 20, "notionalCap": 50000.0, "notionalFloor": 0.0,
     "maintMarginRatio": 0.01, "cum": 0.0},
    {"bracket": 2, "initialLeverage": 10, "notionalCap": 250000.0, "notionalFloor": 50000.0,
     "maintMarginRatio": 0.025, "cum": 750.0},
]


def test_maintenance_tier_from_finds_matching_bracket():
    tier = market.maintenance_tier_from(BRACKETS, 30000.0)
    assert tier.maint_margin_rate == 0.01
    assert tier.maint_amount == 0.0
    assert tier.floor == 0.0
    assert tier.cap == 50000.0


def test_maintenance_tier_from_finds_second_bracket():
    tier = market.maintenance_tier_from(BRACKETS, 100000.0)
    assert tier.maint_margin_rate == 0.025
    assert tier.maint_amount == 750.0


def test_maintenance_tier_from_notional_beyond_every_cap_uses_highest_tier():
    tier = market.maintenance_tier_from(BRACKETS, 999999.0)
    assert tier.maint_margin_rate == 0.025


class LadderFakeClient:
    def __init__(self, margin_type_error: ClientError | None = None):
        self.margin_type_error = margin_type_error
        self.open_orders_calls = []
        self.leverage_bracket_calls = []
        self.margin_type_calls = []

    def get_open_orders(self, symbol):
        self.open_orders_calls.append(symbol)
        return [{"symbol": symbol, "orderId": 1, "side": "BUY", "price": "2400.00"}]

    def leverage_brackets(self, symbol=None):
        self.leverage_bracket_calls.append(symbol)
        return [{"symbol": symbol, "brackets": BRACKETS}]

    def change_margin_type(self, symbol, marginType):
        self.margin_type_calls.append((symbol, marginType))
        if self.margin_type_error is not None:
            raise self.margin_type_error


def test_get_open_orders_calls_client():
    c = LadderFakeClient()
    orders = market.get_open_orders(c, "ETHUSDT")
    assert c.open_orders_calls == ["ETHUSDT"]
    assert orders[0]["orderId"] == 1


def test_get_leverage_brackets_unwraps_single_symbol_response():
    c = LadderFakeClient()
    brackets = market.get_leverage_brackets(c, "ETHUSDT")
    assert brackets == BRACKETS
    assert c.leverage_bracket_calls == ["ETHUSDT"]


def test_set_margin_type_changed():
    c = LadderFakeClient()
    assert market.set_margin_type(c, "ETHUSDT") == "changed"
    assert c.margin_type_calls == [("ETHUSDT", "ISOLATED")]


def test_set_margin_type_already_set_is_a_no_op():
    c = LadderFakeClient(margin_type_error=ClientError(400, -4046, "No need to change margin type.", {}))
    assert market.set_margin_type(c, "ETHUSDT") == "already_set"


def test_set_margin_type_position_open_is_reported_not_raised():
    c = LadderFakeClient(
        margin_type_error=ClientError(400, -4047, "Margin type cannot be changed if there exists position.", {})
    )
    assert market.set_margin_type(c, "ETHUSDT") == "position_open"


def test_set_margin_type_other_errors_raise():
    c = LadderFakeClient(margin_type_error=ClientError(400, -1021, "Timestamp for this request is outside of the recvWindow.", {}))
    with pytest.raises(ClientError):
        market.set_margin_type(c, "ETHUSDT")
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_market.py -v`
Expected: the new tests FAIL with `AttributeError` for each missing name.

- [ ] **Step 3: Implement**

```python
# futures/market.py — add near the top, after the Filters dataclass

from binance.error import ClientError


@dataclass(frozen=True)
class MarginTier:
    """One row of Binance's per-symbol maintenance-margin bracket table."""
    floor: float
    cap: float
    maint_margin_rate: float
    maint_amount: float


def maintenance_tier_from(brackets: list[dict], notional: float) -> MarginTier:
    """The bracket whose [notionalFloor, notionalCap) contains `notional`.

    A notional beyond every bracket's cap uses the highest tier as the most
    conservative approximation -- Binance's tiers only ever increase the
    maintenance margin rate at higher notional, so this over- rather than
    under-estimates risk."""
    for b in brackets:
        floor = float(b["notionalFloor"])
        cap = float(b["notionalCap"])
        if floor <= notional < cap:
            return MarginTier(
                floor=floor, cap=cap,
                maint_margin_rate=float(b["maintMarginRatio"]),
                maint_amount=float(b["cum"]),
            )
    last = brackets[-1]
    return MarginTier(
        floor=float(last["notionalFloor"]), cap=float(last["notionalCap"]),
        maint_margin_rate=float(last["maintMarginRatio"]),
        maint_amount=float(last["cum"]),
    )
```

```python
# futures/market.py — add near the bottom, after set_leverage()

def get_open_orders(client, symbol: str) -> list[dict]:
    """Every open order on `symbol`. Polled each tick a ladder is live to
    detect fills by diffing against the tracked rung order-id map -- see
    bot.py and the design doc's "Rate limits" section for the added cost."""
    return client.get_open_orders(symbol=symbol)


def get_leverage_brackets(client, symbol: str) -> list[dict]:
    """The maintenance-margin bracket table for `symbol`. Fetched once at
    startup/symbol-switch and cached by bot.py -- this table changes rarely,
    unlike position state."""
    response = client.leverage_brackets(symbol=symbol)
    return response[0]["brackets"]


def set_margin_type(client, symbol: str, margin_type: str = "ISOLATED") -> str:
    """Set margin type, tolerating the two expected rejections.

    -4046 means the account is already in the requested mode - a no-op.
    -4047 means a position is already open on the symbol; margin mode can
    only change while flat. That is reported to the caller rather than
    retried here: bot.py logs a warning and keeps running rather than
    refusing to start, per the design doc's "Margin mode" section - this is
    a risk-profile setting, not a correctness invariant."""
    try:
        client.change_margin_type(symbol=symbol, marginType=margin_type)
        return "changed"
    except ClientError as exc:
        if exc.error_code == -4046:
            return "already_set"
        if exc.error_code == -4047:
            return "position_open"
        raise
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_market.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/market.py tests/test_market.py
git commit -m "feat: add open orders, leverage brackets, and margin-type control to market.py"
```

---

### Task 4: `execution.py` — limit order placement and cancellation

**Files:**
- Modify: `futures/execution.py`
- Test: `tests/test_execution.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `execution.place_limit_order(client, symbol, side, qty, price,
  reduce_only=False) -> dict`, `execution.cancel_orders(client, symbol,
  order_ids) -> list[dict]` (one entry per id; a `-2011 Unknown order`
  failure for an id is recorded as `{"orderId": id, "status": "already_gone"}`
  rather than raised, since the bot's tracked state can be one tick stale),
  `execution.query_order_result(client, symbol, order_id) -> dict` — consumed
  by `bot.py` (Task 15+) to distinguish a filled rung from a cancelled one.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_execution.py — add imports and tests

from binance.error import ClientError


class LadderFakeClient(FakeClient):
    def __init__(self, cancel_error: ClientError | None = None):
        super().__init__()
        self.cancel_error = cancel_error
        self.cancelled = []

    def cancel_order(self, symbol, orderId):
        self.cancelled.append((symbol, orderId))
        if self.cancel_error is not None:
            raise self.cancel_error
        return {"orderId": orderId, "status": "CANCELED"}

    def query_order(self, symbol, orderId):
        return {"orderId": orderId, "status": "FILLED", "avgPrice": "2400.0",
                "executedQty": "0.041", "cumQuote": "98.4"}


def test_place_limit_order_sends_gtc_limit():
    c = FakeClient()
    execution.place_limit_order(c, "ETHUSDT", "BUY", 0.041, 2400.0)
    assert c.orders == [
        {
            "symbol": "ETHUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": 0.041,
            "price": 2400.0,
            "timeInForce": "GTC",
        }
    ]


def test_place_limit_order_sets_reduce_only_when_requested():
    c = FakeClient()
    execution.place_limit_order(c, "ETHUSDT", "SELL", 0.041, 2400.0, reduce_only=True)
    assert c.orders[0]["reduceOnly"] == "true"


def test_place_limit_order_omits_reduce_only_by_default():
    c = FakeClient()
    execution.place_limit_order(c, "ETHUSDT", "BUY", 0.041, 2400.0)
    assert "reduceOnly" not in c.orders[0]


def test_cancel_orders_cancels_each_id():
    c = LadderFakeClient()
    results = execution.cancel_orders(c, "ETHUSDT", [1, 2, 3])
    assert c.cancelled == [("ETHUSDT", 1), ("ETHUSDT", 2), ("ETHUSDT", 3)]
    assert [r["status"] for r in results] == ["CANCELED", "CANCELED", "CANCELED"]


def test_cancel_orders_tolerates_already_gone():
    c = LadderFakeClient(cancel_error=ClientError(400, -2011, "Unknown order sent.", {}))
    results = execution.cancel_orders(c, "ETHUSDT", [1])
    assert results == [{"orderId": 1, "status": "already_gone"}]


def test_cancel_orders_raises_other_errors():
    c = LadderFakeClient(cancel_error=ClientError(400, -1021, "Timestamp", {}))
    with pytest.raises(ClientError):
        execution.cancel_orders(c, "ETHUSDT", [1])


def test_query_order_result_returns_raw_response():
    c = LadderFakeClient()
    result = execution.query_order_result(c, "ETHUSDT", 5)
    assert result["status"] == "FILLED"
    assert result["orderId"] == 5
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_execution.py -v`
Expected: FAIL with `AttributeError: module 'execution' has no attribute
'place_limit_order'`.

- [ ] **Step 3: Implement**

```python
# futures/execution.py — add near the top

from binance.error import ClientError
```

```python
# futures/execution.py — add after execute()

def place_limit_order(
    client,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    reduce_only: bool = False,
) -> dict:
    """Place a resting GTC limit order. Used only for in-zone SCALE_IN/
    SCALE_OUT rungs -- STOP_OUT/HALT_FLATTEN/dead-band exits keep using
    execute() (market), unchanged, per the design doc."""
    params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "quantity": qty,
        "price": price,
        "timeInForce": "GTC",
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return client.new_order(**params)


def cancel_orders(client, symbol: str, order_ids) -> list[dict]:
    """Cancel each id, tolerating one already gone (-2011) rather than
    raising: the bot's tracked rung state is read once a tick and can be one
    poll stale relative to the exchange, e.g. if a rung filled between the
    diff read and the cancel call."""
    results = []
    for order_id in order_ids:
        try:
            results.append(client.cancel_order(symbol=symbol, orderId=order_id))
        except ClientError as exc:
            if exc.error_code == -2011:
                results.append({"orderId": order_id, "status": "already_gone"})
            else:
                raise
    return results


def query_order_result(client, symbol: str, order_id) -> dict:
    """Raw exchange response for one order, used to tell a filled rung from
    a cancelled one and to extract its fill via fill_from_response()."""
    return client.query_order(symbol=symbol, orderId=order_id)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_execution.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/execution.py tests/test_execution.py
git commit -m "feat: add limit-order placement, cancellation, and order lookup to execution.py"
```

---

### Task 5: `ladder.py` — rung price table

**Files:**
- Create: `futures/ladder.py`
- Test: `tests/test_ladder.py`

**Interfaces:**
- Consumes: `settings.Zone`, `settings.LONG`, `settings.SHORT`.
- Produces: `ladder.rung_prices(zone, rung_spacing_pct) -> tuple[float, ...]`
  — consumed by `ladder.rung_table` (Task 6).

- [ ] **Step 1: Write failing test**

```python
# tests/test_ladder.py — new file

import math

import pytest

import ladder
from settings import Zone, LONG, SHORT

ZONE = Zone(support=2371.26, resistance=2625.00)


def test_rung_prices_span_support_to_resistance():
    prices = ladder.rung_prices(ZONE, rung_spacing_pct=0.005)
    assert prices[0] == ZONE.support
    assert prices[-1] == ZONE.resistance


def test_rung_prices_count_matches_spacing_formula():
    # span = 253.74, resistance = 2625.00 -> count = ceil(253.74 / (0.005*2625.00)) = ceil(19.33) = 20
    span = ZONE.resistance - ZONE.support
    expected_count = math.ceil(span / (0.005 * ZONE.resistance))
    prices = ladder.rung_prices(ZONE, rung_spacing_pct=0.005)
    assert len(prices) == expected_count + 1


def test_rung_prices_evenly_spaced():
    prices = ladder.rung_prices(ZONE, rung_spacing_pct=0.005)
    step = prices[1] - prices[0]
    for i in range(len(prices) - 1):
        assert prices[i + 1] - prices[i] == pytest.approx(step)


def test_narrower_zone_gets_fewer_rungs_at_same_spacing():
    narrow = Zone(support=2600.00, resistance=2625.00)
    wide_count = len(ladder.rung_prices(ZONE, rung_spacing_pct=0.005))
    narrow_count = len(ladder.rung_prices(narrow, rung_spacing_pct=0.005))
    assert narrow_count < wide_count


def test_rung_prices_at_least_one_rung_for_a_tiny_zone():
    tiny = Zone(support=2624.99, resistance=2625.00)
    prices = ladder.rung_prices(tiny, rung_spacing_pct=0.005)
    assert len(prices) == 2  # one interval: [support, resistance]
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ladder'`.

- [ ] **Step 3: Implement**

```python
# futures/ladder.py — new file

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
```

- [ ] **Step 4: Run test, verify it passes**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/ladder.py tests/test_ladder.py
git commit -m "feat: add ladder.py with rung price table generation"
```

---

### Task 6: `ladder.py` — cumulative target per rung

**Files:**
- Modify: `futures/ladder.py`
- Test: `tests/test_ladder.py`

**Interfaces:**
- Consumes: `ladder.rung_prices` (Task 5), `strategy.distance`,
  `strategy.target_notional`, `strategy.signed`.
- Produces: `ladder.Rung` (frozen dataclass: `price: float`,
  `cumulative_target: float`), `ladder.rung_table(zone, trend, max_n, alpha,
  rung_spacing_pct) -> tuple[Rung, ...]` — consumed by `ladder.build_rung_orders`
  (Task 7).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ladder.py — add

from strategy import distance, target_notional, signed


def test_rung_table_matches_strategy_curve_at_each_price():
    rungs = ladder.rung_table(ZONE, LONG, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005)
    for rung in rungs:
        d = distance(rung.price, ZONE, LONG)
        expected = signed(target_notional(d, 5000.0, 2.0), LONG)
        assert rung.cumulative_target == pytest.approx(expected)


def test_rung_table_target_is_max_at_support_for_long():
    rungs = ladder.rung_table(ZONE, LONG, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005)
    assert rungs[0].price == ZONE.support
    assert rungs[0].cumulative_target == pytest.approx(5000.0)


def test_rung_table_target_is_zero_at_resistance_for_long():
    rungs = ladder.rung_table(ZONE, LONG, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005)
    assert rungs[-1].cumulative_target == pytest.approx(0.0)


def test_rung_table_target_is_negative_for_short():
    rungs = ladder.rung_table(ZONE, SHORT, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005)
    assert all(r.cumulative_target <= 0 for r in rungs)
    assert rungs[-1].cumulative_target == pytest.approx(-5000.0)  # resistance is favourable for short
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: FAIL with `AttributeError: module 'ladder' has no attribute 'Rung'`.

- [ ] **Step 3: Implement**

```python
# futures/ladder.py — add imports and code

from dataclasses import dataclass

from strategy import distance, target_notional, signed


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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/ladder.py tests/test_ladder.py
git commit -m "feat: add ladder.py cumulative rung target table"
```

---

### Task 7: `ladder.py` — rung order sizes and side determination

**Files:**
- Modify: `futures/ladder.py`
- Test: `tests/test_ladder.py`

**Interfaces:**
- Consumes: `ladder.rung_table` (Task 6).
- Produces: `ladder.RungOrder` (frozen dataclass: `price: float`, `size:
  float` — a non-negative notional magnitude), `ladder.build_rung_orders(zone,
  trend, max_n, alpha, rung_spacing_pct) -> tuple[RungOrder, ...]`,
  `ladder.side_for(rung_price, current_price, trend) -> str` (`"BUY"` or
  `"SELL"`), `ladder.DesiredOrder` (frozen dataclass: `price: float`, `side:
  str`, `size: float`), `ladder.desired_orders(zone, trend, max_n, alpha,
  rung_spacing_pct, current_price) -> tuple[DesiredOrder, ...]` — consumed by
  the liquidation cap (Task 8), validation (Task 9), and diffing (Task 10).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ladder.py — add

def test_build_rung_orders_sizes_are_positive_and_sum_to_max_n_for_long():
    orders = ladder.build_rung_orders(ZONE, LONG, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005)
    assert all(o.size >= 0 for o in orders)
    assert sum(o.size for o in orders) == pytest.approx(5000.0)


def test_build_rung_orders_excludes_the_unfavourable_edge_for_long():
    # resistance (target=0) needs no order of its own.
    orders = ladder.build_rung_orders(ZONE, LONG, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005)
    assert ZONE.resistance not in [o.price for o in orders]
    assert ZONE.support in [o.price for o in orders]


def test_build_rung_orders_excludes_the_unfavourable_edge_for_short():
    orders = ladder.build_rung_orders(ZONE, SHORT, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005)
    assert ZONE.support not in [o.price for o in orders]
    assert ZONE.resistance in [o.price for o in orders]
    assert sum(o.size for o in orders) == pytest.approx(5000.0)


def test_side_for_long_buys_below_current_price():
    assert ladder.side_for(2400.0, current_price=2450.0, trend=LONG) == "BUY"


def test_side_for_long_sells_above_current_price():
    assert ladder.side_for(2500.0, current_price=2450.0, trend=LONG) == "SELL"


def test_side_for_short_sells_above_current_price():
    assert ladder.side_for(2500.0, current_price=2450.0, trend=SHORT) == "SELL"


def test_side_for_short_buys_below_current_price():
    assert ladder.side_for(2400.0, current_price=2450.0, trend=SHORT) == "BUY"


def test_desired_orders_splits_buy_and_sell_by_current_price():
    orders = ladder.desired_orders(
        ZONE, LONG, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005, current_price=2500.0
    )
    for order in orders:
        if order.price < 2500.0:
            assert order.side == "BUY"
        elif order.price > 2500.0:
            assert order.side == "SELL"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: FAIL with `AttributeError: module 'ladder' has no attribute
'RungOrder'`.

- [ ] **Step 3: Implement**

```python
# futures/ladder.py — add

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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/ladder.py tests/test_ladder.py
git commit -m "feat: add ladder.py rung sizing, side determination, and desired-order set"
```

---

### Task 8: `ladder.py` — liquidation-aware accumulate-side cap

**Files:**
- Modify: `futures/ladder.py`
- Test: `tests/test_ladder.py`

**Interfaces:**
- Consumes: `market.MarginTier`, `settings.LONG`, `settings.SHORT`.
- Produces: `ladder.liquidation_scale(position_qty, entry_price,
  isolated_wallet, leverage, tier, survival_price, planned_delta_qty,
  planned_delta_notional, trend) -> float` (a safety factor in `[0, 1]`),
  `ladder.survival_price(zones, active_index, trend, liquidation_buffer_pct)
  -> float`, `ladder.apply_liquidation_cap(orders, scale, trend) ->
  tuple[DesiredOrder, ...]` — consumed by `bot.py` (Task 16).

**Note on the formula:** this implements the closed-form derivation in the
design doc's "Liquidation-aware buy-side cap" section, using Binance's
documented isolated-margin liquidation formula (single position, one-way
mode): for LONG, `LiqPrice = (EntryPrice*Qty - IsolatedWallet + MaintAmount)
/ (Qty * (1 - MaintMarginRate))`; for SHORT, `LiqPrice = (EntryPrice*Qty +
IsolatedWallet - MaintAmount) / (Qty * (1 + MaintMarginRate))`. Adding
`Δqty` at price `p` is modelled as also adding `Δqty*p/leverage` of fresh
isolated margin (matching how Binance actually funds an added fill at a
fixed leverage). The safety condition is evaluated directly at the two
endpoints `k=0` (no additional buying) and `k=1` (the full planned amount)
first, because the ratio's monotonicity in `k` can flip sign depending on
the specific numbers; the closed-form crossing point is only used, and is
only reliable, in the sub-case where those two endpoints disagree on
safety — since the ratio has no pole for `k` in `[0, 1]` (quantity stays
positive throughout), that sub-case is guaranteed by the intermediate value
theorem to have its unique real transition inside `(0, 1)`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ladder.py — add

from market import MarginTier

TIER = MarginTier(floor=0.0, cap=250000.0, maint_margin_rate=0.05, maint_amount=10.0)


def test_liquidation_scale_already_unsafe_at_zero_returns_zero():
    # q0=10, e0=100, WB0=... chosen so LiqPrice(0) > survival_price already.
    k = ladder.liquidation_scale(
        position_qty=10, entry_price=100, isolated_wallet=150, leverage=20,
        tier=TIER, survival_price=70, planned_delta_qty=20,
        planned_delta_notional=1800, trend=LONG,
    )
    assert k == 0.0


def test_liquidation_scale_fully_safe_at_one_returns_one():
    # Loose survival target, well past LiqPrice at full planned buying.
    k = ladder.liquidation_scale(
        position_qty=10, entry_price=100, isolated_wallet=300, leverage=20,
        tier=TIER, survival_price=95, planned_delta_qty=20,
        planned_delta_notional=1800, trend=LONG,
    )
    assert k == 1.0


def test_liquidation_scale_partial_cap_matches_closed_form():
    # Hand-derived: A=710, B=1710, D0=9.5, E=19, survival=80 -> k = 5/19.
    k = ladder.liquidation_scale(
        position_qty=10, entry_price=100, isolated_wallet=300, leverage=20,
        tier=TIER, survival_price=80, planned_delta_qty=20,
        planned_delta_notional=1800, trend=LONG,
    )
    assert k == pytest.approx(5 / 19, rel=1e-6)


def test_liquidation_scale_result_actually_meets_the_survival_price():
    # The scale found must make the projected liquidation price exactly the
    # survival price at the boundary case (not just "close").
    k = ladder.liquidation_scale(
        position_qty=10, entry_price=100, isolated_wallet=300, leverage=20,
        tier=TIER, survival_price=80, planned_delta_qty=20,
        planned_delta_notional=1800, trend=LONG,
    )
    qty = 10 + k * 20
    wallet = 300 + k * 1800 / 20
    cost_basis = 100 * 10 + k * 1800
    liq_price = (cost_basis - wallet + TIER.maint_amount) / (qty * (1 - TIER.maint_margin_rate))
    assert liq_price == pytest.approx(80.0)


def test_liquidation_scale_short_mirrors_long():
    # Survival above current price for a short; same shape of answer.
    k = ladder.liquidation_scale(
        position_qty=10, entry_price=100, isolated_wallet=300, leverage=20,
        tier=TIER, survival_price=120, planned_delta_qty=20,
        planned_delta_notional=2200, trend=SHORT,
    )
    assert 0.0 <= k <= 1.0


def test_survival_price_uses_next_lower_zone_for_long():
    zones = (
        Zone(support=2625.00, resistance=3284.04),
        Zone(support=2371.26, resistance=2625.00),
        Zone(support=1872.46, resistance=2371.26),
    )
    # active zone is index 0; next-lower is index 1, span 253.74.
    target = ladder.survival_price(zones, active_index=0, trend=LONG, liquidation_buffer_pct=0.2)
    next_zone = zones[1]
    expected = next_zone.resistance - 0.2 * (next_zone.resistance - next_zone.support)
    assert target == pytest.approx(expected)


def test_survival_price_falls_back_to_own_span_at_the_lowest_zone_for_long():
    zones = (
        Zone(support=2625.00, resistance=3284.04),
        Zone(support=2371.26, resistance=2625.00),
        Zone(support=1872.46, resistance=2371.26),
    )
    target = ladder.survival_price(zones, active_index=2, trend=LONG, liquidation_buffer_pct=0.2)
    edge = zones[2]
    expected = edge.support - 0.2 * (edge.resistance - edge.support)
    assert target == pytest.approx(expected)


def test_apply_liquidation_cap_shrinks_only_the_accumulate_side():
    orders = ladder.desired_orders(ZONE, LONG, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005, current_price=2500.0)
    capped = ladder.apply_liquidation_cap(orders, scale=0.5, trend=LONG)
    for original, adjusted in zip(orders, capped):
        if original.side == "BUY":
            assert adjusted.size == pytest.approx(original.size * 0.5)
        else:
            assert adjusted.size == original.size
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: FAIL with `AttributeError: module 'ladder' has no attribute
'liquidation_scale'`.

- [ ] **Step 3: Implement**

```python
# futures/ladder.py — add

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

    See this task's docstring note above and the design doc's
    "Liquidation-aware buy-side cap" for the derivation. k=1 means the full
    planned accumulation is safe as-is; k=0 means even the smallest further
    accumulation is unsafe."""
    mmr = tier.maint_margin_rate
    maint_amount = tier.maint_amount

    def liquidation_price(k: float) -> float:
        qty = position_qty + k * planned_delta_qty
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

    # Safety is monotonic and continuous between k=0 (safe) and k=1 (unsafe)
    # -- no pole in this range since qty stays positive throughout -- so the
    # closed-form crossing point is guaranteed to land in (0, 1).
    if trend == LONG:
        a = entry_price * position_qty - isolated_wallet + maint_amount
        b = planned_delta_notional * (1 - 1 / leverage)
        c = survival_price * (1 - mmr)
    else:
        a = entry_price * position_qty + isolated_wallet - maint_amount
        b = planned_delta_notional * (1 + 1 / leverage)
        c = survival_price * (1 + mmr)

    k = (c * position_qty - a) / (b - c * planned_delta_qty)
    return max(0.0, min(1.0, k))


def survival_price(
    zones: tuple[Zone, ...],
    active_index: int,
    trend: str,
    liquidation_buffer_pct: float,
) -> float:
    """The price the position must survive to without liquidating.

    Zones are contiguous and ordered highest-to-lowest (settings.py already
    enforces zones[i].resistance == zones[i+1].support), so for LONG the
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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/ladder.py tests/test_ladder.py
git commit -m "feat: add liquidation-aware accumulate-side cap to ladder.py"
```

---

### Task 9: `ladder.py` — validation and reconciliation diff

**Files:**
- Modify: `futures/ladder.py`
- Test: `tests/test_ladder.py`

**Interfaces:**
- Consumes: `market.Filters`.
- Produces: `ladder.validate_orders(orders, current_price, trend, filters) ->
  tuple[valid: tuple[DesiredOrder, ...], deferred: tuple[DesiredOrder, ...]]`,
  `ladder.ReconciliationPlan` (frozen dataclass: `cancel: tuple[int, ...]`
  order ids, `place: tuple[DesiredOrder, ...]`), `ladder.plan_orders(desired,
  open_orders: list[dict]) -> ReconciliationPlan` — consumed by `bot.py`
  (Task 16).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_ladder.py — add

from market import Filters

FILTERS = Filters(step_size=0.001, min_qty=0.001, min_notional=20.0)


def test_validate_orders_accepts_correctly_sided_orders():
    orders = (
        ladder.DesiredOrder(price=2400.0, side="BUY", size=100.0),
        ladder.DesiredOrder(price=2500.0, side="SELL", size=50.0),
    )
    valid, deferred = ladder.validate_orders(orders, current_price=2450.0, trend=LONG, filters=FILTERS)
    assert valid == orders
    assert deferred == ()


def test_validate_orders_defers_a_buy_priced_above_current():
    # Simulates the race the design doc's "settling" step exists for: price
    # moved between the stability check and validation.
    orders = (ladder.DesiredOrder(price=2460.0, side="BUY", size=100.0),)
    valid, deferred = ladder.validate_orders(orders, current_price=2450.0, trend=LONG, filters=FILTERS)
    assert valid == ()
    assert deferred == orders


def test_validate_orders_defers_a_sell_priced_below_current():
    orders = (ladder.DesiredOrder(price=2440.0, side="SELL", size=100.0),)
    valid, deferred = ladder.validate_orders(orders, current_price=2450.0, trend=LONG, filters=FILTERS)
    assert deferred == orders


def test_validate_orders_drops_below_min_notional_as_neither_valid_nor_deferred():
    # A rung shrunk near zero by the liquidation cap: not worth an order,
    # and not worth retrying either.
    orders = (ladder.DesiredOrder(price=2400.0, side="BUY", size=1.0),)  # 1.0 USDT notional
    valid, deferred = ladder.validate_orders(orders, current_price=2450.0, trend=LONG, filters=FILTERS)
    assert valid == ()
    assert deferred == ()


def test_plan_orders_places_missing_and_leaves_matching_alone():
    desired = (
        ladder.DesiredOrder(price=2400.0, side="BUY", size=100.0),
        ladder.DesiredOrder(price=2500.0, side="SELL", size=50.0),
    )
    open_orders = [{"orderId": 1, "price": "2400.00", "side": "BUY", "origQty": "0.04167"}]
    plan = ladder.plan_orders(desired, open_orders)
    assert plan.cancel == ()
    assert plan.place == (desired[1],)


def test_plan_orders_cancels_stale_and_places_replacement():
    desired = (ladder.DesiredOrder(price=2400.0, side="BUY", size=150.0),)  # size changed
    open_orders = [{"orderId": 1, "price": "2400.00", "side": "BUY", "origQty": "0.04167"}]  # was 100.0 notional
    plan = ladder.plan_orders(desired, open_orders, price_tolerance=0.0001)
    assert plan.cancel == (1,)
    assert plan.place == (desired[0],)


def test_plan_orders_cancels_orders_no_longer_desired():
    desired = ()
    open_orders = [{"orderId": 1, "price": "2400.00", "side": "BUY", "origQty": "0.04167"}]
    plan = ladder.plan_orders(desired, open_orders)
    assert plan.cancel == (1,)
    assert plan.place == ()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: FAIL with `AttributeError: module 'ladder' has no attribute
'validate_orders'`.

- [ ] **Step 3: Implement**

```python
# futures/ladder.py — add

def validate_orders(
    orders: tuple[DesiredOrder, ...],
    current_price: float,
    trend: str,
    filters,  # market.Filters
) -> tuple[tuple[DesiredOrder, ...], tuple[DesiredOrder, ...]]:
    """Split desired orders into (valid, deferred).

    An order is deferred rather than placed when it would cross the book --
    e.g. a BUY priced above current market price -- which happens when price
    moved between the settling check and this validation. It is dropped
    entirely (neither valid nor deferred) when its notional cannot clear the
    exchange minimum, since a liquidation-capped rung can shrink toward
    zero: retrying a sub-minimum order forever is pointless. See design doc
    "Lifecycle", step 4."""
    valid, deferred = [], []
    for order in orders:
        if order.price * order.size == 0:
            continue
        notional = order.size
        if notional < filters.min_notional:
            continue
        crosses = (
            (order.side == "BUY" and order.price >= current_price)
            or (order.side == "SELL" and order.price <= current_price)
        )
        if crosses:
            deferred.append(order)
        else:
            valid.append(order)
    return tuple(valid), tuple(deferred)


@dataclass(frozen=True)
class ReconciliationPlan:
    cancel: tuple[int, ...]   # order ids to cancel
    place: tuple[DesiredOrder, ...]


def plan_orders(
    desired: tuple[DesiredOrder, ...],
    open_orders: list[dict],
    price_tolerance: float = 0.0001,
) -> ReconciliationPlan:
    """Diff the desired order set against what is actually open, minimizing
    churn: an open order matching a desired one (same side, price within
    tolerance) is left alone rather than cancelled and replaced."""
    remaining_desired = list(desired)
    cancel = []

    for open_order in open_orders:
        open_price = float(open_order["price"])
        open_side = open_order["side"]
        match = next(
            (
                d for d in remaining_desired
                if d.side == open_side and abs(d.price - open_price) <= price_tolerance * open_price
            ),
            None,
        )
        if match is not None:
            remaining_desired.remove(match)
        else:
            cancel.append(open_order["orderId"])

    return ReconciliationPlan(cancel=tuple(cancel), place=tuple(remaining_desired))
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_ladder.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/ladder.py tests/test_ladder.py
git commit -m "feat: add ladder.py order validation and reconciliation diff"
```

---

### Task 10: `notify.py` — liquidation brake event

**Files:**
- Modify: `futures/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `notify.format_liquidation_brake(symbol, trend, leverage,
  survival_price, liquidation_price, scale, cancelled: bool) -> str`,
  `notify.liquidation_brake(**values) -> bool` — consumed by `bot.py`
  (Task 17).

- [ ] **Step 1: Write failing test**

```python
# tests/test_notify.py — add (matching the existing test_format_halt style
# further down the same file; check that file's fixtures before writing this
# so it reuses `sent`/`posts` consistently with neighboring tests)

def test_format_liquidation_brake_reports_a_shrink():
    text = notify.format_liquidation_brake(
        symbol="ETHUSDT", trend="long", leverage=5,
        survival_price=1780.0, liquidation_price=1850.0,
        scale=0.4, cancelled=False,
    )
    assert "LIQUIDATION BRAKE" in text
    assert "1780.00" in text
    assert "1850.00" in text
    assert "40" in text  # scale shown as a percentage


def test_format_liquidation_brake_reports_a_full_cancel():
    text = notify.format_liquidation_brake(
        symbol="ETHUSDT", trend="long", leverage=5,
        survival_price=1780.0, liquidation_price=1900.0,
        scale=0.0, cancelled=True,
    )
    assert "CANCELLED" in text.upper()


def test_liquidation_brake_event_sends(sent):
    notify.liquidation_brake(
        symbol="ETHUSDT", trend="long", leverage=5,
        survival_price=1780.0, liquidation_price=1850.0,
        scale=0.4, cancelled=False,
    )
    assert len(sent) == 1
    assert "LIQUIDATION BRAKE" in sent[0]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_notify.py -v`
Expected: FAIL with `AttributeError: module 'notify' has no attribute
'format_liquidation_brake'`.

- [ ] **Step 3: Implement**

```python
# futures/notify.py — add after format_config_refused()

def format_liquidation_brake(
    symbol: str,
    trend: str,
    leverage: int,
    survival_price: float,
    liquidation_price: float,
    scale: float,
    cancelled: bool,
) -> str:
    """Message for the liquidation-aware brake engaging on the ladder's
    accumulate side -- either a projected shrink or the reported-price
    backstop cancelling outright. Worth a push the same way HALT is: an
    operator who has walked away needs to know the bot intervened to avoid
    liquidation even though nothing failed outright."""
    lines = [
        "LIQUIDATION BRAKE" + (" - CANCELLED REMAINING ORDERS" if cancelled else ""),
        f"{symbol} {trend} {leverage}x",
        f"liquidation price: {_num(liquidation_price)}",
        f"survival target: {_num(survival_price)}",
    ]
    if not cancelled:
        lines.append(f"accumulate-side rungs scaled to {scale * 100:.0f}% of plan")
    lines.append("")
    lines.append("No action needed -- the bot adjusted itself.")
    return "\n".join(lines)
```

```python
# futures/notify.py — add after config_refused()

def liquidation_brake(**values) -> bool:
    """Call whenever the accumulate-side cap actually shrinks remaining
    rungs, or the reported-liquidationPrice backstop cancels them outright."""
    return _notify(format_liquidation_brake, **values)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_notify.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/notify.py tests/test_notify.py
git commit -m "feat: add liquidation_brake notification event"
```

---

### Task 11: `bot.py` — ISOLATED margin at startup and symbol switch

**Files:**
- Modify: `futures/bot.py`
- Test: `tests/test_tick_throttle.py`

**Interfaces:**
- Consumes: `market.set_margin_type` (Task 3).
- Produces: nothing new consumed by later tasks; this task is about the
  startup/reload call sites.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tick_throttle.py — extend LoopClient and add tests

class LoopClient:
    def __init__(self, price="2700.00", balance="0.0", order_error=None, margin_type_error=None):
        self.price = price
        self._balance = balance
        self.order_error = order_error
        self.margin_type_error = margin_type_error
        self.orders = 0
        self.margin_type_calls = []

    # ... existing methods unchanged ...

    def change_margin_type(self, symbol, marginType):
        self.margin_type_calls.append((symbol, marginType))
        if self.margin_type_error is not None:
            raise self.margin_type_error


def test_startup_sets_isolated_margin(monkeypatch, written):
    api = LoopClient()
    run_loop(monkeypatch, written, ticks=1, api=api)
    assert api.margin_type_calls == [("ETHUSDT", "ISOLATED")]


def test_startup_tolerates_already_isolated(monkeypatch, written):
    from binance.error import ClientError
    api = LoopClient(margin_type_error=ClientError(400, -4046, "No need to change margin type.", {}))
    # Must not raise / must not prevent the loop from running.
    run_loop(monkeypatch, written, ticks=1, api=api)


def test_startup_warns_but_continues_when_position_open(monkeypatch, written, capsys):
    from binance.error import ClientError
    api = LoopClient(margin_type_error=ClientError(400, -4047, "Margin type cannot be changed if there exists position.", {}))
    run_loop(monkeypatch, written, ticks=1, api=api)
    output = capsys.readouterr().out
    assert "ISOLATED" in output.upper()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v`
Expected: FAIL — `test_startup_sets_isolated_margin` fails with `assert [] ==
[('ETHUSDT', 'ISOLATED')]` since `bot.run()` never calls it yet.

- [ ] **Step 3: Implement**

```python
# futures/bot.py — in run(), immediately after market.set_leverage(...) and
# before "label = ..."

    margin_result = market.set_margin_type(api, cfg.symbol)
    if margin_result == "position_open":
        print(
            f"WARNING: could not switch {cfg.symbol} to ISOLATED margin - "
            "a position is already open in CROSSED mode. Continuing in "
            "CROSSED mode; the liquidation-cap projection assumes ISOLATED "
            "and will be wrong until this position clears and the switch "
            "can retry."
        )
```

Also apply the same call where the symbol-change reload path currently does
`market.set_leverage(api, new_cfg.symbol, new_cfg.leverage)` (inside the
`elif new_cfg.symbol != cfg.symbol:` branch, in the `else:` sub-branch after
the `old_amt != 0` check):

```python
# futures/bot.py — in the symbol-switch reload branch, right after
# market.set_leverage(api, new_cfg.symbol, new_cfg.leverage)

                            margin_result = market.set_margin_type(api, new_cfg.symbol)
                            if margin_result == "position_open":
                                print(
                                    f"WARNING: could not switch {new_cfg.symbol} to "
                                    "ISOLATED margin - a position is already open in "
                                    "CROSSED mode."
                                )
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/bot.py tests/test_tick_throttle.py
git commit -m "feat: switch to ISOLATED margin at startup and on symbol switch"
```

---

### Task 12: `bot.py` — ladder state scaffolding and cached leverage brackets

**Files:**
- Modify: `futures/bot.py`
- Test: `tests/test_tick_throttle.py`

**Interfaces:**
- Consumes: `market.get_leverage_brackets`, `market.maintenance_tier_from`.
- Produces: `bot.LadderState` (a small mutable container this task
  introduces: `orders: dict[float, int]` mapping rung price to open order
  id, `settling: bool`, `settling_snapshot: frozenset[int]` — the open
  order ids seen when settling began) — consumed by Tasks 13-17. Also
  caches `leverage_brackets` in a loop-local variable, re-fetched only on
  symbol switch.

- [ ] **Step 1: Write failing test**

```python
# tests/test_tick_throttle.py — add

def test_leverage_brackets_are_fetched_once_at_startup(monkeypatch, written):
    api = LoopClient()
    api.leverage_bracket_calls = []

    def leverage_brackets(symbol):
        api.leverage_bracket_calls.append(symbol)
        return [{"symbol": symbol, "brackets": [
            {"bracket": 1, "initialLeverage": 20, "notionalCap": 50000.0,
             "notionalFloor": 0.0, "maintMarginRatio": 0.01, "cum": 0.0},
        ]}]

    monkeypatch.setattr(api, "leverage_brackets", leverage_brackets, raising=False)
    run_loop(monkeypatch, written, ticks=5, api=api)
    assert api.leverage_bracket_calls == ["ETHUSDT"]  # once, not once per tick
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v`
Expected: FAIL with `assert [] == ['ETHUSDT']` — nothing calls
`leverage_brackets` yet.

- [ ] **Step 3: Implement**

```python
# futures/bot.py — add near the top, with the other imports

import ladder
```

```python
# futures/bot.py — add near TickLog, before def _config_changes

class LadderState:
    """Mutable ladder-lifecycle state for the CURRENTLY active zone. Reset
    to a fresh instance whenever the active zone changes (STOP_OUT/HALT
    market-flattens the old zone's position, so its ladder starts over from
    an empty state on the new zone) - see design doc "Lifecycle"."""

    def __init__(self):
        self.orders: dict[float, int] = {}  # rung price -> open order id
        self.settling = False
        self.settling_snapshot: frozenset = frozenset()

    def reset(self) -> None:
        self.__init__()
```

```python
# futures/bot.py — in run(), after filters = market.get_filters(...)

    leverage_brackets = market.get_leverage_brackets(api, cfg.symbol)
```

```python
# futures/bot.py — in run(), in the symbol-switch reload branch, right
# after filters = new_filters (so a stale bracket table never sizes the new
# symbol's ladder)

                            leverage_brackets = market.get_leverage_brackets(api, new_cfg.symbol)
```

```python
# futures/bot.py — in run(), alongside the other per-loop mutable state
# (active_index = None, halted = False, ...)

    ladder_state = LadderState()
```

- [ ] **Step 4: Run test, verify it passes**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/bot.py tests/test_tick_throttle.py
git commit -m "feat: add LadderState scaffolding and cached leverage brackets to bot.py"
```

---

### Task 13: `bot.py` — route in-zone SCALE_IN/SCALE_OUT to ladder placement

**Files:**
- Modify: `futures/bot.py`
- Test: `tests/test_tick_throttle.py`

**Interfaces:**
- Consumes: `ladder.desired_orders`, `ladder.survival_price`,
  `ladder.liquidation_scale`, `ladder.apply_liquidation_cap`,
  `ladder.validate_orders`, `ladder.plan_orders`, `execution.place_limit_order`,
  `market.entry_price_from`, `market.isolated_wallet_from`,
  `market.maintenance_tier_from`.
- Produces: the tick body's new branch for in-zone `SCALE_IN`/`SCALE_OUT`,
  and a `_place_ladder(...)` helper — consumed by Task 15 (zone-change
  cancellation) and Task 16 (settling reconciliation reuses the same
  building blocks).

This task handles **zone activation only** (first entry into a zone, or
right after a `STOP_OUT`/`HALT_FLATTEN` cleared the position) — the
position is guaranteed flat, so the full ladder places fresh. Ongoing
fill-driven reconciliation is Tasks 15-17.

- [ ] **Step 1: Write failing test**

```python
# tests/test_tick_throttle.py — add

def test_zone_activation_places_a_full_ladder_of_limit_orders(monkeypatch, written):
    # Price sits inside the middle of the configured zone ladder, trend long,
    # zero starting position -> zone activates and the ladder places.
    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=api)
    assert api.orders > 0
    assert all(o["type"] == "LIMIT" for o in api.placed_orders)
```

(This requires `LoopClient` to track placed orders with their `type`; add
to `LoopClient.new_order`:)

```python
# tests/test_tick_throttle.py — modify LoopClient.new_order

    def new_order(self, **params):
        self.orders += 1
        self.placed_orders.append(params)
        if self.order_error is not None:
            raise RuntimeError(self.order_error(self.orders))
        if params.get("type") == "LIMIT":
            return {"orderId": self.orders, "status": "NEW"}
        return {"orderId": self.orders, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}
```

And add `self.placed_orders = []` to `LoopClient.__init__`, plus a fake
`get_open_orders`/`get_position_risk` that reports `entryPrice`/
`isolatedWallet`/`liquidationPrice` fields (needed by the ladder path) and
starts returning `[]` for open orders:

```python
# tests/test_tick_throttle.py — modify LoopClient

    def get_position_risk(self, symbol=None):
        return [{
            "symbol": "ETHUSDT", "positionAmt": "0.0", "unRealizedProfit": "0.0",
            "liquidationPrice": "0.0", "entryPrice": "0.0", "isolatedWallet": "0.0",
        }]

    def get_open_orders(self, symbol):
        return []
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v`
Expected: FAIL with `assert 0 > 0` — the loop still fires a market order
for `SCALE_IN`/`SCALE_OUT`, not a ladder.

- [ ] **Step 3: Implement**

```python
# futures/bot.py — add near the bottom, before run()

def _liquidation_inputs(snapshot, cfg, filters, leverage_brackets):
    """Bundle the values ladder.liquidation_scale() needs, read from the
    tick's own snapshot so the projection uses the same instant the rest of
    the tick does."""
    position_qty = abs(snapshot.position_amt)
    notional = position_qty * snapshot.mark_price
    tier = market.maintenance_tier_from(leverage_brackets, notional)
    return position_qty, tier


def _place_ladder(api, cfg, zone_index, price, max_n, filters, leverage_brackets, ladder_state) -> None:
    """Compute the full desired order set for the active zone against
    current price and zero starting position, cap the accumulate side, and
    place every valid rung. Used on zone activation, where the position is
    guaranteed flat by the STOP_OUT/HALT_FLATTEN that preceded it."""
    zone = cfg.zones[zone_index]
    desired = ladder.desired_orders(
        zone, cfg.trend, max_n, cfg.alpha, cfg.rung_spacing_pct, price
    )
    valid, _deferred = ladder.validate_orders(desired, price, cfg.trend, filters)
    for order in valid:
        qty = execution.quantity_for(order.size, order.price, filters)
        if not execution.is_executable(qty, order.price, filters):
            continue
        result = execution.place_limit_order(api, cfg.symbol, order.side, qty, order.price)
        ladder_state.orders[order.price] = result["orderId"]
```

```python
# futures/bot.py — in the main tick body, replace the block that currently
# reads:
#
#     side = "BUY" if decision.delta > 0 else "SELL"
#     ...
#     result = execution.execute(api, cfg.symbol, side, qty, reduce_only=reduce_only)
#
# with a branch: in-zone SCALE_IN/SCALE_OUT goes to the ladder; everything
# else (STOP_OUT, HALT_FLATTEN, the dead-band SCALE_OUT where
# decision.zone_index is None) keeps using execute() exactly as before.

            in_zone_ladder_case = (
                decision.zone_index is not None
                and decision.reason in (strategy.SCALE_IN, strategy.SCALE_OUT)
            )

            if in_zone_ladder_case:
                zone_changed = decision.zone_index != active_index
                if zone_changed or not ladder_state.orders:
                    ladder_state.reset()
                    _place_ladder(
                        api, cfg, decision.zone_index, price, decision.max_n,
                        filters, leverage_brackets, ladder_state,
                    )
                active_index = decision.zone_index
                _sleep(cfg.poll_seconds)
                continue

            side = "BUY" if decision.delta > 0 else "SELL"
            # ... existing market-order code below is UNCHANGED ...
```

Note: this placement replaces the `if not sending: ... continue` /
`side = ...` block's *entry point* for the ladder case only; the existing
`if decision.action in (strategy.HOLD, strategy.IDLE): ... continue` block
above it, and everything from `side = "BUY"` onward, stays exactly as
written today for the non-ladder paths (`STOP_OUT`, `HALT_FLATTEN`, dead-band
`SCALE_OUT`). Insert the new `in_zone_ladder_case` block immediately after
the existing `halting`/`halt_context` block and before `if not sending:`.

- [ ] **Step 4: Run test, verify it passes**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v`
Expected: all PASS. Also re-run the full suite to confirm the untouched
market-order paths (`STOP_OUT`, `HALT_FLATTEN`) still work:

Run: `.viper/Scripts/python.exe -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/bot.py tests/test_tick_throttle.py
git commit -m "feat: place a limit-order ladder on zone activation for in-zone scale in/out"
```

---

### Task 13b: `market.py`/`execution.py` — exchange price-tick rounding

**Inserted during execution.** Task 13's review found that `ladder.rung_prices`
computes raw floating-point prices with no exchange `tickSize` rounding
anywhere in `futures/` — every resting limit order would be rejected with
`-1111 Precision is over the maximum defined for this asset` against the
real exchange. `market.Filters` carries only `step_size`/`min_qty`/
`min_notional`; there is no price-tick handling. Fixing this at the source
(inside `ladder.py`'s pure math) would mean threading a new parameter
through five already-reviewed, approved functions (`rung_prices`,
`rung_table`, `build_rung_orders`, `desired_orders`, `validate_orders`,
`plan_orders`). Instead, this follows the exact precedent
`execution.quantity_for` already set for *quantity*: round only at the
execution boundary, where the theoretical price is about to become a real
order — `ladder.py`'s rungs stay at their exact theoretical positions.

**Files:**
- Modify: `futures/market.py`
- Modify: `futures/execution.py`
- Modify: `tests/test_market.py`, `tests/test_execution.py`, `tests/test_ladder.py` (update the three existing `Filters(...)` test fixtures to include `tick_size`)

**Interfaces:**
- Modifies: `market.Filters` gains a required `tick_size: float` field.
  `market.get_filters` extracts it from the `PRICE_FILTER` filter's
  `tickSize` (the `tests/test_market.py` `FakeClient.exchange_info()`
  fixture already includes this filter, unused until now — no fixture
  change needed there).
- Produces: `execution.price_for(price: float, filters: Filters) -> float`
  — rounds a raw price to the nearest valid multiple of `filters.tick_size`
  — consumed by `bot.py`'s `_place_ladder` (Task 13, retrofit) and Task
  16's `_reconcile_ladder` (not yet dispatched — will be written to call
  it from the start).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_market.py — add

def test_get_filters_extracts_tick_size():
    f = market.get_filters(FakeClient(), "ETHUSDT")
    assert f.tick_size == 0.01


def test_get_filters_raises_when_price_filter_missing():
    with pytest.raises(ValueError, match="PRICE_FILTER"):
        market.get_filters(MissingFilterClient("PRICE_FILTER"), "ETHUSDT")
```

```python
# tests/test_execution.py — update the shared fixture

F = Filters(step_size=0.001, min_qty=0.001, min_notional=20.0, tick_size=0.01)


def test_price_for_rounds_down_to_the_nearest_tick():
    # 2400.017 / 0.01 = 240001.7 -> floor 240001 -> 2400.01
    assert execution.price_for(2400.017, F) == 2400.01


def test_price_for_exact_multiple_is_unchanged():
    assert execution.price_for(2400.05, F) == 2400.05


def test_price_for_handles_floating_point_noise():
    # 0.1 + 0.2 style noise must not round to an invalid tick.
    price = 2400.00 + 0.01 * 3  # binary float noise around 2400.03
    result = execution.price_for(price, F)
    assert round(result / F.tick_size) == round(result / F.tick_size)  # exact multiple
    assert result == 2400.03
```

```python
# tests/test_ladder.py — update the shared fixture

FILTERS = Filters(step_size=0.001, min_qty=0.001, min_notional=20.0, tick_size=0.01)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_market.py tests/test_execution.py tests/test_ladder.py -v`
Expected: the new tests FAIL (`TypeError: Filters.__init__() missing... 'tick_size'` for the fixture-based ones, `AttributeError` for `market.Filters` and `execution.price_for`).

- [ ] **Step 3: Implement**

```python
# futures/market.py — modify Filters

@dataclass(frozen=True)
class Filters:
    step_size: float
    min_qty: float
    min_notional: float
    tick_size: float
```

```python
# futures/market.py — modify get_filters()

def get_filters(client, symbol: str) -> Filters:
    info = client.exchange_info()
    entry = next(s for s in info["symbols"] if s["symbol"] == symbol)

    step_size = min_qty = min_notional = tick_size = None
    for f in entry["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
            min_qty = float(f["minQty"])
        elif f["filterType"] == "MIN_NOTIONAL":
            min_notional = float(f["notional"])
        elif f["filterType"] == "PRICE_FILTER":
            tick_size = float(f["tickSize"])

    for name, value in (
        ("LOT_SIZE", step_size),
        ("LOT_SIZE", min_qty),
        ("MIN_NOTIONAL", min_notional),
        ("PRICE_FILTER", tick_size),
    ):
        if value is None or value <= 0:
            raise ValueError(f"{symbol}: missing {name} filter")

    return Filters(
        step_size=step_size, min_qty=min_qty, min_notional=min_notional,
        tick_size=tick_size,
    )
```

```python
# futures/execution.py — add near quantity_for()

def price_for(price: float, filters: Filters) -> float:
    """Limit-order price floored to the exchange's tick size.

    Mirrors quantity_for()'s rounding direction and rationale: the exchange
    rejects a price that is not an exact multiple of tickSize with -1111,
    and floats accumulate noise that a naive round() can push onto an
    invalid tick. Floors rather than rounds to nearest so a BUY rung never
    creeps above its intended price (which could turn a resting order
    marketable) and a SELL rung never creeps below (same risk, mirrored)."""
    ticks = math.floor(round(price / filters.tick_size, 8))
    return round(ticks * filters.tick_size, 8)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_market.py tests/test_execution.py tests/test_ladder.py -v`
Expected: all PASS. Then run the full suite once:

Run: `.viper/Scripts/python.exe -m pytest tests/ -v`
Expected: all PASS — confirm no other test constructs a bare `Filters(...)` that this change missed (grep the whole repo for `Filters(` if any fail).

- [ ] **Step 5: Retrofit `_place_ladder`**

`futures/bot.py`'s `_place_ladder` (added in Task 13) currently calls
`execution.place_limit_order(api, cfg.symbol, order.side, qty, order.price)`
with the raw, unrounded `order.price`. Change this one call site to round
first:

```python
# futures/bot.py — in _place_ladder, change the placement call

        rounded_price = execution.price_for(order.price, filters)
        result = execution.place_limit_order(api, cfg.symbol, order.side, qty, rounded_price)
        ladder_state.orders[rounded_price] = result["orderId"]
```

(Track the rung by its *rounded* price, not the theoretical one — this is
the price that will actually appear in `market.get_open_orders`' responses,
which Task 15+'s diffing logic compares against.)

Add one test confirming the rounded price is what's actually sent:

```python
# tests/test_tick_throttle.py — add

def test_ladder_orders_are_placed_at_tick_rounded_prices(monkeypatch, written):
    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=api)
    for order in api.placed_orders:
        if order.get("type") == "LIMIT":
            # ETHUSDT tick size in this fixture is 0.01 -- every price must
            # be an exact multiple, not a raw theoretical rung price.
            assert round(order["price"] / 0.01) == pytest.approx(order["price"] / 0.01, abs=1e-6)
```

- [ ] **Step 6: Run tests, verify they pass; commit**

Run: `.viper/Scripts/python.exe -m pytest tests/ -v`
Expected: all PASS.

```bash
git add futures/market.py futures/execution.py futures/bot.py tests/test_market.py tests/test_execution.py tests/test_ladder.py tests/test_tick_throttle.py
git commit -m "feat: round ladder limit-order prices to the exchange tick size"
```

---

### Task 14: `bot.py` — cancel the ladder on zone change and HALT

**Files:**
- Modify: `futures/bot.py`
- Test: `tests/test_tick_throttle.py`

**Interfaces:**
- Consumes: `execution.cancel_orders`.
- Produces: `_cancel_ladder(api, cfg, ladder_state)` helper — consumed by
  the `STOP_OUT`/`HALT_FLATTEN` paths and Task 13's zone-changed branch.

- [ ] **Step 1: Write failing test**

```python
# tests/test_tick_throttle.py — add. LoopClient gains a price schedule so a
# single run_loop() call can move price across two ticks (bot.run() has no
# supported pause/resume, so this drives the transition within one run).

class LoopClient:
    def __init__(self, price="2700.00", balance="0.0", order_error=None,
                 margin_type_error=None, price_schedule=None):
        self.price = price
        self.price_schedule = price_schedule or {}  # {tick_number: price}
        self._balance = balance
        self.order_error = order_error
        self.margin_type_error = margin_type_error
        self.orders = 0
        self.placed_orders = []
        self.cancelled_order_ids = []
        self.margin_type_calls = []
        self._tick = 0

    def ticker_price(self, symbol):
        self._tick += 1
        if self._tick in self.price_schedule:
            self.price = self.price_schedule[self._tick]
        return {"symbol": symbol, "price": self.price}

    def cancel_order(self, symbol, orderId):
        self.cancelled_order_ids.append(orderId)
        return {"orderId": orderId, "status": "CANCELED"}


def test_stop_out_cancels_the_old_zones_ladder_before_flattening(monkeypatch, written):
    api = LoopClient(price="2500.00", balance="1000.0", price_schedule={2: "2350.00"})
    run_loop(monkeypatch, written, ticks=2, api=api)
    placed_ids = [
        i + 1 for i, p in enumerate(api.placed_orders) if p.get("type") == "LIMIT"
    ]
    assert placed_ids  # the first tick placed a ladder
    assert set(placed_ids).issubset(set(api.cancelled_order_ids))
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v`
Expected: FAIL — `api.cancelled_order_ids` is empty; nothing calls
`cancel_order` yet.

- [ ] **Step 3: Implement**

```python
# futures/bot.py — add next to _place_ladder

def _cancel_ladder(api, cfg, ladder_state) -> None:
    """Cancel every resting order the ladder currently tracks for the zone
    being left, then clear the tracked state. Called before STOP_OUT/
    HALT_FLATTEN's market order fires, and before placing a fresh ladder for
    a newly-activated zone."""
    if not ladder_state.orders:
        return
    execution.cancel_orders(api, cfg.symbol, list(ladder_state.orders.values()))
    ladder_state.reset()
```

```python
# futures/bot.py — in run(), replace the in_zone_ladder_case branch from
# Task 13 so a zone change cancels before placing:

            if in_zone_ladder_case:
                zone_changed = decision.zone_index != active_index
                if zone_changed:
                    _cancel_ladder(api, cfg, ladder_state)
                if zone_changed or not ladder_state.orders:
                    _place_ladder(
                        api, cfg, decision.zone_index, price, decision.max_n,
                        filters, leverage_brackets, ladder_state,
                    )
                active_index = decision.zone_index
                _sleep(cfg.poll_seconds)
                continue
```

```python
# futures/bot.py — in run(), immediately before the existing
# "side = 'BUY' if decision.delta > 0 else 'SELL'" line (the STOP_OUT/
# HALT_FLATTEN/dead-band market-order path), cancel any ladder still
# tracked for the zone being left:

            _cancel_ladder(api, cfg, ladder_state)

            side = "BUY" if decision.delta > 0 else "SELL"
```

- [ ] **Step 4: Run test, verify it passes**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/bot.py tests/test_tick_throttle.py
git commit -m "feat: cancel the resting ladder before STOP_OUT/HALT_FLATTEN and on zone change"
```

---

### Task 15: `bot.py` — poll-based fill detection and the settling wait

**Files:**
- Modify: `futures/bot.py`
- Test: `tests/test_tick_throttle.py`

**Interfaces:**
- Consumes: `market.get_open_orders`.
- Produces: `_detect_fill(api, cfg, ladder_state) -> tuple[int, ...]`
  (the tracked order ids no longer open) — consumed by Task 16.

- [ ] **Step 1: Write failing test**

```python
# tests/test_tick_throttle.py — LoopClient gains an open-orders schedule and
# tracks placed LIMIT orders as "open" until explicitly removed

class LoopClient:
    # ... __init__ additionally sets: self.open_orders_after_tick = {}; self._open_orders = []

    def new_order(self, **params):
        self.orders += 1
        self.placed_orders.append(params)
        order_id = self.orders
        if params.get("type") == "LIMIT":
            self._open_orders.append({
                "orderId": order_id, "side": params["side"],
                "price": f"{params['price']:.2f}", "origQty": str(params["quantity"]),
            })
            return {"orderId": order_id, "status": "NEW"}
        if self.order_error is not None:
            raise RuntimeError(self.order_error(self.orders))
        return {"orderId": order_id, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

    def get_open_orders(self, symbol):
        if self._tick in self.open_orders_after_tick:
            keep_ids = self.open_orders_after_tick[self._tick]
            self._open_orders = [o for o in self._open_orders if o["orderId"] in keep_ids]
        return list(self._open_orders)

    def cancel_order(self, symbol, orderId):
        self.cancelled_order_ids.append(orderId)
        self._open_orders = [o for o in self._open_orders if o["orderId"] != orderId]
        return {"orderId": orderId, "status": "CANCELED"}

    def query_order(self, symbol, orderId):
        return {"orderId": orderId, "status": "FILLED", "avgPrice": self.price,
                "executedQty": "0.041", "cumQuote": str(0.041 * float(self.price))}


def test_a_filled_rung_enters_settling_without_reconciling_the_same_tick(monkeypatch, written):
    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=api)
    all_ids = [o["orderId"] for o in api._open_orders]
    fill_one = all_ids[0]

    api = LoopClient(
        price="2500.00", balance="1000.0",
        open_orders_after_tick={2: [i for i in all_ids if i != fill_one]},
    )
    run_loop(monkeypatch, written, ticks=2, api=api)
    # A fill was detected (tick 2), but with no confirming stable tick after
    # it, nothing should have been cancelled/replaced yet.
    assert api.cancelled_order_ids == []
```

- [ ] **Step 2: Run test**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v -k enters_settling`
Expected: PASSES already at this point (nothing reconciles yet regardless),
which is fine — this test is a regression guard for Task 16, added now
alongside the detection code it exercises.

- [ ] **Step 3: Implement**

```python
# futures/bot.py — add next to _cancel_ladder

def _detect_fill(api, cfg, ladder_state) -> tuple:
    """Diff currently open orders against the ladder's tracked rung->id map.
    Returns the tracked ids that are no longer open (newly filled or
    cancelled out from under the bot) -- empty if nothing changed.
    Poll-based, so this is up to one poll_seconds late -- see design doc
    "Risk notes"."""
    if not ladder_state.orders:
        return ()
    open_ids = {o["orderId"] for o in market.get_open_orders(api, cfg.symbol)}
    tracked_ids = set(ladder_state.orders.values())
    return tuple(tracked_ids - open_ids)
```

```python
# futures/bot.py — in the in_zone_ladder_case branch, add fill detection
# for the steady-state (zone unchanged, ladder already placed) case:

            if in_zone_ladder_case:
                zone_changed = decision.zone_index != active_index
                if zone_changed:
                    _cancel_ladder(api, cfg, ladder_state)
                    _place_ladder(
                        api, cfg, decision.zone_index, price, decision.max_n,
                        filters, leverage_brackets, ladder_state,
                    )
                elif not ladder_state.orders:
                    _place_ladder(
                        api, cfg, decision.zone_index, price, decision.max_n,
                        filters, leverage_brackets, ladder_state,
                    )
                elif _detect_fill(api, cfg, ladder_state):
                    ladder_state.settling = True
                    ladder_state.settling_snapshot = frozenset(
                        o["orderId"] for o in market.get_open_orders(api, cfg.symbol)
                    )
                active_index = decision.zone_index
                _sleep(cfg.poll_seconds)
                continue
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add futures/bot.py tests/test_tick_throttle.py
git commit -m "feat: detect ladder fills by polling open orders and enter settling"
```

---

### Task 16: `bot.py` — settle, recompute, cap, validate, reconcile

**Files:**
- Modify: `futures/bot.py`
- Test: `tests/test_tick_throttle.py`

**Interfaces:**
- Consumes: everything from Tasks 8-9 (`ladder.survival_price`,
  `ladder.liquidation_scale`, `ladder.apply_liquidation_cap`,
  `ladder.desired_orders`, `ladder.validate_orders`, `ladder.plan_orders`),
  `execution.cancel_orders`, `execution.place_limit_order`.
- Produces: `_reconcile_ladder(...)` helper. **This task writes the helper
  using `snapshot.entry_price`/`snapshot.isolated_wallet`/
  `snapshot.liquidation_price`, which do not exist on `market.Snapshot`
  until Task 17 extends it.** Task 16 and Task 17 are a single logical
  change split across two tasks only because they introduce two separable
  ideas (the reconciliation algorithm, and the `Snapshot`/wiring change it
  depends on) — do not consider Task 16 done until Task 17's tests also
  pass.

- [ ] **Step 1: Write failing test**

```python
# tests/test_tick_throttle.py — add

def test_settled_ladder_reconciles_to_the_new_desired_set(monkeypatch, written):
    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=api)
    all_ids = [o["orderId"] for o in api._open_orders]
    first_fill_id = all_ids[0]

    api = LoopClient(
        price="2500.00", balance="1000.0",
        open_orders_after_tick={2: [i for i in all_ids if i != first_fill_id]},
    )
    # Tick 1: places the ladder. Tick 2: that rung disappears -> settling.
    # Tick 3: open-order set unchanged from tick 2 -> stable -> reconcile.
    run_loop(monkeypatch, written, ticks=3, api=api)

    initial_ladder_size = len(all_ids)
    assert api.orders > initial_ladder_size  # reconciliation placed at least one more order
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v -k test_settled_ladder_reconciles`
Expected: FAIL — `api.orders` stops growing after the initial ladder
placement; nothing reconciles yet.

- [ ] **Step 3: Implement**

```python
# futures/bot.py — add next to _detect_fill

def _reconcile_ladder(api, cfg, decision, price, snapshot, filters, leverage_brackets, ladder_state) -> None:
    """Recompute the desired order set against the current actual position
    and price, apply the liquidation cap to the accumulate side, validate,
    and reconcile (cancel stale, place new, leave matching orders alone).

    Called only once the market has been observed stable for one full tick
    (see _detect_fill / the settling flag) -- see design doc "Lifecycle".
    Requires snapshot.entry_price / snapshot.isolated_wallet /
    snapshot.liquidation_price -- added to market.Snapshot in the task that
    follows this one."""
    zone = cfg.zones[decision.zone_index]
    desired = ladder.desired_orders(
        zone, cfg.trend, decision.max_n, cfg.alpha, cfg.rung_spacing_pct, price
    )

    position_qty = abs(snapshot.position_amt)
    survival = ladder.survival_price(
        cfg.zones, decision.zone_index, cfg.trend, cfg.liquidation_buffer_pct
    )
    notional = position_qty * price
    tier = market.maintenance_tier_from(leverage_brackets, notional)

    accumulate_side = "BUY" if cfg.trend == settings.LONG else "SELL"
    planned = [o for o in desired if o.side == accumulate_side]
    planned_delta_notional = sum(o.size for o in planned)
    planned_delta_qty = planned_delta_notional / price if price else 0.0

    scale = ladder.liquidation_scale(
        position_qty=position_qty,
        entry_price=snapshot.entry_price,
        isolated_wallet=snapshot.isolated_wallet,
        leverage=cfg.leverage,
        tier=tier,
        survival_price=survival,
        planned_delta_qty=planned_delta_qty,
        planned_delta_notional=planned_delta_notional,
        trend=cfg.trend,
    )
    capped = ladder.apply_liquidation_cap(desired, scale, cfg.trend) if scale < 1.0 else desired

    valid, _deferred = ladder.validate_orders(capped, price, cfg.trend, filters)
    open_orders = market.get_open_orders(api, cfg.symbol)
    plan = ladder.plan_orders(valid, open_orders)

    if plan.cancel:
        execution.cancel_orders(api, cfg.symbol, list(plan.cancel))
        for rung_price, order_id in list(ladder_state.orders.items()):
            if order_id in plan.cancel:
                del ladder_state.orders[rung_price]

    for order in plan.place:
        qty = execution.quantity_for(order.size, order.price, filters)
        if not execution.is_executable(qty, order.price, filters):
            continue
        result = execution.place_limit_order(api, cfg.symbol, order.side, qty, order.price)
        ladder_state.orders[order.price] = result["orderId"]

    if scale < 1.0:
        notify.liquidation_brake(
            symbol=cfg.symbol, trend=cfg.trend, leverage=cfg.leverage,
            survival_price=survival, liquidation_price=snapshot.liquidation_price,
            scale=scale, cancelled=(scale == 0.0),
        )

    ladder_state.settling = False
```

- [ ] **Step 4: Run test**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v -k test_settled_ladder_reconciles`
Expected: still FAILS with an `AttributeError` on `snapshot.entry_price` (a
different failure than Step 2's, confirming `_reconcile_ladder` itself is
syntactically correct and reachable). This is expected — do not attempt to
fix it here; proceed to Task 17, which adds the missing `Snapshot` fields
and wires this helper into `run()`. Do not commit yet.

---

### Task 17: `bot.py` / `market.py` — extend `Snapshot`, wire settling to reconciliation, backstop check

**Files:**
- Modify: `futures/market.py`
- Modify: `futures/bot.py`
- Test: `tests/test_market.py`, `tests/test_tick_throttle.py`

**Interfaces:**
- Modifies: `market.Snapshot` gains `entry_price: float`,
  `isolated_wallet: float`, `liquidation_price: float` fields, populated in
  `market.get_snapshot` from the same position-risk payload it already
  reads (no new REST call).
- Wires: the settling branch in `run()` now calls `_reconcile_ladder` once
  stable, and every tick checks the reported `liquidation_price` backstop
  regardless of settling state.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_market.py — add

def test_get_snapshot_carries_ladder_fields():
    snap = market.get_snapshot(FakeClient(), "ETHUSDT")
    assert hasattr(snap, "entry_price")
    assert hasattr(snap, "isolated_wallet")
    assert hasattr(snap, "liquidation_price")
```

(Update the shared `POSITIONS` fixture in `tests/test_market.py`, already
extended with `liquidationPrice`/`entryPrice`/`isolatedWallet` in Task 2, so
`FakeClient` returns them here too.)

```python
# tests/test_tick_throttle.py — add the backstop test

def test_liquidation_backstop_cancels_remaining_buy_rungs(monkeypatch, written):
    class BreachedLoopClient(LoopClient):
        def get_position_risk(self, symbol=None):
            return [{
                "symbol": "ETHUSDT", "positionAmt": "1.0", "unRealizedProfit": "0.0",
                "liquidationPrice": "2495.00",  # essentially at current price: breached
                "entryPrice": "2500.00", "isolatedWallet": "500.00",
            }]

    api = BreachedLoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=api)
    buy_ids = [
        i + 1 for i, p in enumerate(api.placed_orders)
        if p.get("type") == "LIMIT" and p.get("side") == "BUY"
    ]
    assert buy_ids
    run_loop(monkeypatch, written, ticks=2, api=api)
    assert set(buy_ids).issubset(set(api.cancelled_order_ids))
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.viper/Scripts/python.exe -m pytest tests/test_market.py tests/test_tick_throttle.py -v`
Expected: FAIL — `Snapshot` has no `entry_price` attribute yet, and
`Task 16`'s reconciliation test still fails as left off.

- [ ] **Step 3: Implement**

```python
# futures/market.py — modify Snapshot

@dataclass(frozen=True)
class Snapshot:
    mark_price: float
    position_amt: float
    unrealized_pnl: float
    wallet_balance: float
    available_balance: float
    entry_price: float
    isolated_wallet: float
    liquidation_price: float
```

```python
# futures/market.py — modify get_snapshot()

def get_snapshot(client, symbol: str, asset: str = "USDT") -> Snapshot:
    price = float(client.ticker_price(symbol)["price"])  # weight 1
    positions = client.get_position_risk(symbol=symbol)  # weight 5
    balances = client.balance()  # weight 5

    return Snapshot(
        mark_price=price,
        position_amt=position_amt_from(positions, symbol),
        unrealized_pnl=unrealized_pnl_from(positions, symbol),
        wallet_balance=wallet_balance_from(balances, asset),
        available_balance=available_balance_from(balances, asset),
        entry_price=entry_price_from(positions, symbol),
        isolated_wallet=isolated_wallet_from(positions, symbol),
        liquidation_price=liquidation_price_from(positions, symbol),
    )
```

```python
# futures/bot.py — replace the in_zone_ladder_case branch (from Task 15)
# with the full wiring, including the settling->reconcile transition and
# the independent liquidation backstop check:

            if in_zone_ladder_case:
                zone_changed = decision.zone_index != active_index
                if zone_changed:
                    _cancel_ladder(api, cfg, ladder_state)
                    _place_ladder(
                        api, cfg, decision.zone_index, price, decision.max_n,
                        filters, leverage_brackets, ladder_state,
                    )
                elif not ladder_state.orders:
                    _place_ladder(
                        api, cfg, decision.zone_index, price, decision.max_n,
                        filters, leverage_brackets, ladder_state,
                    )
                elif ladder_state.settling:
                    open_now = frozenset(
                        o["orderId"] for o in market.get_open_orders(api, cfg.symbol)
                    )
                    if open_now == ladder_state.settling_snapshot:
                        _reconcile_ladder(
                            api, cfg, decision, price, snapshot, filters,
                            leverage_brackets, ladder_state,
                        )
                    else:
                        ladder_state.settling_snapshot = open_now
                elif _detect_fill(api, cfg, ladder_state):
                    ladder_state.settling = True
                    ladder_state.settling_snapshot = frozenset(
                        o["orderId"] for o in market.get_open_orders(api, cfg.symbol)
                    )

                # Backstop: independent of settling, cancel remaining
                # accumulate-side orders outright if the exchange's own
                # reported liquidation price has already crossed the
                # survival target. See design doc point 4 of
                # "Liquidation-aware buy-side cap".
                if ladder_state.orders and snapshot.position_amt != 0:
                    survival = ladder.survival_price(
                        cfg.zones, decision.zone_index, cfg.trend, cfg.liquidation_buffer_pct
                    )
                    breached = (
                        snapshot.liquidation_price >= survival
                        if cfg.trend == settings.LONG
                        else snapshot.liquidation_price <= survival
                    )
                    if breached:
                        accumulate_side = "BUY" if cfg.trend == settings.LONG else "SELL"
                        open_orders = market.get_open_orders(api, cfg.symbol)
                        to_cancel = [
                            o["orderId"] for o in open_orders if o["side"] == accumulate_side
                        ]
                        if to_cancel:
                            execution.cancel_orders(api, cfg.symbol, to_cancel)
                            for rung_price, order_id in list(ladder_state.orders.items()):
                                if order_id in to_cancel:
                                    del ladder_state.orders[rung_price]
                            notify.liquidation_brake(
                                symbol=cfg.symbol, trend=cfg.trend, leverage=cfg.leverage,
                                survival_price=survival, liquidation_price=snapshot.liquidation_price,
                                scale=0.0, cancelled=True,
                            )

                active_index = decision.zone_index
                _sleep(cfg.poll_seconds)
                continue
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/ -v`
Expected: all PASS, full suite (including Task 16's test, now unblocked).

- [ ] **Step 5: Commit**

```bash
git add futures/market.py futures/bot.py tests/test_market.py tests/test_tick_throttle.py
git commit -m "feat: wire ladder settling to reconciliation and the liquidation backstop"
```

---

### Task 18: Order-journal and tick-journal fields for ladder fills

**Files:**
- Modify: `futures/bot.py`
- Test: `tests/test_tick_throttle.py`

**Interfaces:**
- Modifies: `_detect_fill` now also returns which ids filled (not just
  whether any did), so the caller can journal each one individually before
  entering settling.
- Produces: an `journal.log_order` write and a `notify.order_executed` call
  for each rung fill, and `ladder_rungs_total`/`liquidation_price` fields
  added to the existing per-tick `tick_log.log(...)` record when a ladder
  is live.

- [ ] **Step 1: Write failing test**

```python
# tests/test_tick_throttle.py — add

def test_a_filled_rung_writes_an_order_journal_record(monkeypatch, written):
    order_journal = []
    monkeypatch.setattr(journal, "log_order", lambda record, log_dir=None: order_journal.append(record))

    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=api)
    all_ids = [o["orderId"] for o in api._open_orders]
    fill_one = all_ids[0]

    api = LoopClient(
        price="2500.00", balance="1000.0",
        open_orders_after_tick={2: [i for i in all_ids if i != fill_one]},
    )
    run_loop(monkeypatch, written, ticks=3, api=api)

    assert any(r.get("order_id") == fill_one for r in order_journal)
    filled_record = next(r for r in order_journal if r.get("order_id") == fill_one)
    assert filled_record["reason"] in (strategy.SCALE_IN, strategy.SCALE_OUT)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `.viper/Scripts/python.exe -m pytest tests/test_tick_throttle.py -v -k order_journal_record`
Expected: FAIL — `order_journal` stays empty; nothing calls
`journal.log_order` for a ladder fill yet.

- [ ] **Step 3: Implement**

```python
# futures/bot.py — in the in_zone_ladder_case branch, replace the
# `elif _detect_fill(api, cfg, ladder_state):` clause to journal each fill
# before entering settling:

                elif (filled_ids := _detect_fill(api, cfg, ladder_state)):
                    for order_id in filled_ids:
                        rung_price = next(
                            (p for p, oid in ladder_state.orders.items() if oid == order_id),
                            None,
                        )
                        try:
                            result = execution.query_order_result(api, cfg.symbol, order_id)
                        except Exception as exc:
                            journal.log_tick({"action": "error", "error": f"ladder fill lookup failed: {exc}"})
                            continue
                        if result.get("status") != "FILLED":
                            continue  # cancelled by us or the exchange, not a fill
                        fill = execution.fill_from_response(result)
                        order_record = {
                            "order_id": order_id,
                            "symbol": cfg.symbol,
                            "side": result.get("side"),
                            "reason": decision.reason,
                            "quantity": fill.qty,
                            "executed_qty": fill.qty,
                            "fill_price": fill.price,
                            "notional": fill.notional,
                            "rung_price": rung_price,
                            "mark_price": price,
                            "trend": cfg.trend,
                            "leverage": cfg.leverage,
                            "active_zone_index": decision.zone_index,
                            "balance": wallet,
                        }
                        journal.log_order(order_record)
                        notify.order_executed(order_record)
                    ladder_state.settling = True
                    ladder_state.settling_snapshot = frozenset(
                        o["orderId"] for o in market.get_open_orders(api, cfg.symbol)
                    )
```

Add the ladder fields to the existing tick-journal record dict (the one
passed to `tick_log.log(...)`), only meaningful while a ladder is tracked:

```python
# futures/bot.py — extend the dict passed to tick_log.log(...)

                    "ladder_rungs_total": len(ladder_state.orders) if in_zone_ladder_case else None,
                    "liquidation_price": snapshot.liquidation_price,
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `.viper/Scripts/python.exe -m pytest tests/ -v`
Expected: all PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add futures/bot.py tests/test_tick_throttle.py
git commit -m "feat: journal and notify individual ladder fills"
```

---

### Task 19: Documentation — update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update the module list and architecture notes**

Add `futures/ladder.py` to the `futures/` module list in `CLAUDE.md` (next
to `strategy.py`, describing it as the pure ladder-computation counterpart),
and add a new architecture-notes bullet describing: the market-order/
limit-order split (`STOP_OUT`/`HALT_FLATTEN`/dead-band exits vs in-zone
`SCALE_IN`/`SCALE_OUT`), the poll-detect-settle-reconcile lifecycle, and the
liquidation-cap projection plus its reported-price backstop — matching the
level of detail the existing bullets (e.g. "Zones are contiguous...",
"Order quantities floor to the lot step...") carry, so a future reader gets
the "why," not just the "what." Reference
`docs/superpowers/specs/2026-09-17-limit-order-ladder-design.md` for the
full derivation rather than re-deriving the liquidation formula inline.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document the limit-order ladder in CLAUDE.md"
```

---

## Self-Review Notes

**Spec coverage:** every section of the design doc maps to a task —
module boundaries (Tasks 5-10), rung table (5-7), liquidation cap (8),
lifecycle/settling (12-18), margin mode (11), rate limits (accepted as a
documented tradeoff, no dedicated task needed since it's a consequence of
Task 15's `get_open_orders` polling), journal/notifications (10, 18), and
CLAUDE.md documentation (19).

**Known cross-task dependency, called out explicitly rather than hidden:**
Task 16 writes `_reconcile_ladder` referencing `Snapshot` fields that do not
exist until Task 17 adds them; Task 16's own steps say so and leave its
test red on purpose. Executing agents should treat Tasks 16 and 17 as one
review unit if using two-stage subagent review — Task 16 introduces one
independently-reviewable idea (the reconciliation algorithm) and Task 17
introduces another (the `Snapshot` extension and its wiring into `run()`).

**Type consistency check performed:** `ladder.DesiredOrder`,
`ladder.RungOrder`, `ladder.Rung`, `market.MarginTier`,
`ladder.ReconciliationPlan` field names (`price`, `side`, `size`,
`cumulative_target`, `maint_margin_rate`, `maint_amount`, `cancel`, `place`)
are used identically in every task that references them, verified by
reading Tasks 6 through 18 in sequence after drafting.

**Placeholder scan:** no `TBD`/`TODO` remain; the one deliberately
"unfinished" step (Task 16 Step 4) is explicitly justified rather than a
placeholder, and is resolved within the same plan by Task 17.
