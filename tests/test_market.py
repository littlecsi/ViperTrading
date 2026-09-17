import pytest

from binance.error import ClientError

import market


BALANCES = [
    {"asset": "USDT", "balance": "1000.0", "availableBalance": "850.5"},
    {"asset": "BNB", "balance": "0.0", "availableBalance": "0.0"},
]

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


class FakeClient:
    def __init__(self):
        self.leverage_calls = []
        self.calls = []

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
        self.calls.append("ticker_price")
        return {"symbol": symbol, "price": "2403.00"}

    def balance(self):
        self.calls.append("balance")
        return [dict(b) for b in BALANCES]

    def get_position_risk(self, symbol=None):
        self.calls.append("get_position_risk")
        return [dict(p) for p in POSITIONS]

    def change_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))
        return {"leverage": leverage, "symbol": symbol}


def test_get_filters():
    f = market.get_filters(FakeClient(), "ETHUSDT")
    assert f.step_size == 0.001
    assert f.min_qty == 0.001
    assert f.min_notional == 20.0


class MissingFilterClient(FakeClient):
    """exchange_info without MIN_NOTIONAL: a renamed filter, a new symbol, or
    testnet diverging from live would all look like this."""

    def __init__(self, drop="MIN_NOTIONAL"):
        super().__init__()
        self.drop = drop

    def exchange_info(self):
        info = super().exchange_info()
        entry = info["symbols"][0]
        entry["filters"] = [f for f in entry["filters"] if f["filterType"] != self.drop]
        return info


def test_get_filters_raises_when_min_notional_missing():
    with pytest.raises(ValueError, match="MIN_NOTIONAL"):
        market.get_filters(MissingFilterClient(), "ETHUSDT")


def test_get_filters_raises_when_lot_size_missing():
    with pytest.raises(ValueError, match="LOT_SIZE"):
        market.get_filters(MissingFilterClient("LOT_SIZE"), "ETHUSDT")


def test_get_filters_extracts_tick_size():
    f = market.get_filters(FakeClient(), "ETHUSDT")
    assert f.tick_size == 0.01


def test_get_filters_raises_when_price_filter_missing():
    with pytest.raises(ValueError, match="PRICE_FILTER"):
        market.get_filters(MissingFilterClient("PRICE_FILTER"), "ETHUSDT")


def test_wallet_balance_uses_total_not_available():
    assert market.wallet_balance_from(BALANCES) == 1000.0


def test_available_balance_from():
    assert market.available_balance_from(BALANCES) == 850.5


def test_balance_unknown_asset_returns_zero():
    assert market.wallet_balance_from(BALANCES, "DOGE") == 0.0
    assert market.available_balance_from(BALANCES, "DOGE") == 0.0


def test_position_amt_from_preserves_sign():
    assert market.position_amt_from(POSITIONS, "ETHUSDT") == -1.250


def test_position_amt_from_unknown_symbol_is_zero():
    assert market.position_amt_from(POSITIONS, "BTCUSDT") == 0.0


def test_unrealized_pnl_from():
    assert market.unrealized_pnl_from(POSITIONS, "ETHUSDT") == -12.5


def test_unrealized_pnl_from_unknown_symbol_is_zero():
    assert market.unrealized_pnl_from(POSITIONS, "BTCUSDT") == 0.0


def test_get_snapshot_carries_every_field():
    snap = market.get_snapshot(FakeClient(), "ETHUSDT")
    assert snap.mark_price == 2403.00
    assert snap.position_amt == -1.250
    assert snap.unrealized_pnl == -12.5
    assert snap.wallet_balance == 1000.0
    assert snap.available_balance == 850.5


def test_get_snapshot_hits_each_endpoint_once():
    """The whole point of the snapshot: 3 calls, 11 request weight. Fetching
    /balance or /positionRisk twice a tick is what put a one-second poll at 53%
    of the rate limit."""
    c = FakeClient()
    market.get_snapshot(c, "ETHUSDT")
    assert sorted(c.calls) == ["balance", "get_position_risk", "ticker_price"]


def test_get_snapshot_unknown_symbol_is_flat():
    snap = market.get_snapshot(FakeClient(), "BTCUSDT")
    assert snap.position_amt == 0.0
    assert snap.unrealized_pnl == 0.0


def test_get_position_amt_preserves_sign():
    assert market.get_position_amt(FakeClient(), "ETHUSDT") == -1.250


def test_get_position_amt_unknown_symbol_is_zero():
    assert market.get_position_amt(FakeClient(), "BTCUSDT") == 0.0


def test_set_leverage_calls_client():
    c = FakeClient()
    market.set_leverage(c, "ETHUSDT", 5)
    assert c.leverage_calls == [("ETHUSDT", 5)]


def test_liquidation_price_from():
    assert market.liquidation_price_from(POSITIONS, "ETHUSDT") == 2950.50


def test_liquidation_price_from_unknown_symbol_is_zero():
    assert market.liquidation_price_from(POSITIONS, "BTCUSDT") == 0.0


def test_entry_price_from():
    assert market.entry_price_from(POSITIONS, "ETHUSDT") == 2400.00


def test_isolated_wallet_from():
    assert market.isolated_wallet_from(POSITIONS, "ETHUSDT") == 600.00


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

    def get_orders(self, symbol):
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
