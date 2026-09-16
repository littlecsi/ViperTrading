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
