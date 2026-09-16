import pytest

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
