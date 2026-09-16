import pytest

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


class FakeClient:
    def __init__(self):
        self.orders = []

    def new_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"orderId": 1, "status": "FILLED", **kwargs}


def test_execute_sends_market_order():
    c = FakeClient()
    result = execution.execute(c, "ETHUSDT", "BUY", 0.041)
    assert c.orders == [
        {
            "symbol": "ETHUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": 0.041,
            "newOrderRespType": "RESULT",
        }
    ]
    assert result["orderId"] == 1


def test_execute_requests_fill_details_in_response():
    # "ACK" (the connector default) carries no avgPrice/executedQty, which is
    # why the order journal could not reconstruct realised PnL.
    c = FakeClient()
    execution.execute(c, "ETHUSDT", "BUY", 0.041)
    assert c.orders[0]["newOrderRespType"] == "RESULT"


def test_execute_omits_reduce_only_by_default():
    c = FakeClient()
    execution.execute(c, "ETHUSDT", "BUY", 0.041)
    assert "reduceOnly" not in c.orders[0]


def test_execute_sets_reduce_only_when_requested():
    c = FakeClient()
    execution.execute(c, "ETHUSDT", "SELL", 0.041, reduce_only=True)
    assert c.orders[0]["reduceOnly"] == "true"


def test_fill_from_response_reads_avg_price():
    fill = execution.fill_from_response(
        {"avgPrice": "2395.16307", "executedQty": "0.876", "cumQuote": "2098.16285"}
    )
    assert fill.price == 2395.16307
    assert fill.qty == 0.876
    assert fill.notional == 2098.16285


def test_fill_from_response_derives_price_from_cum_quote():
    # A futures MARKET response can report executedQty while avgPrice is still
    # "0.00"; cumQuote still carries the true notional.
    fill = execution.fill_from_response(
        {"avgPrice": "0.00", "executedQty": "0.876", "cumQuote": "2098.16285"}
    )
    assert fill.qty == 0.876
    assert fill.notional == 2098.16285
    assert fill.price == pytest.approx(2098.16285 / 0.876)


def test_fill_from_response_derives_notional_when_only_price_known():
    fill = execution.fill_from_response({"avgPrice": "2400.0", "executedQty": "0.5"})
    assert fill.notional == 1200.0


def test_fill_from_response_reports_unknown_fill_as_none():
    # An ACK-shaped response carries nothing usable; recording None keeps the
    # schema honest instead of substituting a pre-trade estimate.
    fill = execution.fill_from_response(
        {"orderId": 1, "status": "NEW", "avgPrice": "0.00", "executedQty": "0", "cumQuote": "0"}
    )
    assert fill == execution.Fill(price=None, qty=None, notional=None)


def test_fill_from_response_tolerates_missing_fields():
    assert execution.fill_from_response({}) == execution.Fill(None, None, None)
