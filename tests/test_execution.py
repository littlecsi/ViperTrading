import pytest
from binance.error import ClientError

import execution
import strategy
from market import Filters

F = Filters(step_size=0.001, min_qty=0.001, min_notional=20.0, tick_size=0.01)


def test_price_for_buy_rounds_down_to_the_nearest_tick():
    # 2400.017 / 0.01 = 240001.7 -> floor 240001 -> 2400.01. A BUY rung sits
    # below current price, so rounding down moves it further away from the
    # market -- the only direction that cannot turn it marketable.
    assert execution.price_for(2400.017, F, "BUY") == 2400.01


def test_price_for_sell_rounds_up_to_the_nearest_tick():
    # 2400.013 / 0.01 = 240001.3 -> ceil 240002 -> 2400.02, not down to
    # 2400.01. A SELL rung sits above current price, so rounding DOWN would
    # move it toward the market -- potentially turning a resting order into
    # an unintended marketable (taker) fill. Rounding up is the mirror of
    # BUY's floor: away from the market on both sides.
    assert execution.price_for(2400.013, F, "SELL") == 2400.02


def test_price_for_exact_multiple_is_unchanged_on_both_sides():
    assert execution.price_for(2400.05, F, "BUY") == 2400.05
    assert execution.price_for(2400.05, F, "SELL") == 2400.05


def test_price_for_buy_handles_floating_point_noise_below_the_tick():
    # 2400.12 / 0.01 == 240011.99999999997 in binary float. A bare
    # math.floor() on that raw quotient truncates to 240011 -> 2400.11, one
    # tick too low; round(..., 8) first recovers the true 240012 -> 2400.12.
    assert execution.price_for(2400.12, F, "BUY") == 2400.12


def test_price_for_sell_handles_floating_point_noise_above_the_tick():
    # 2400.00 + 0.01*3 == 240003.00000000003 in binary float. A bare
    # math.ceil() on that raw quotient rounds up to 240004 -> 2400.04, one
    # tick too high; round(..., 8) first recovers the true 240003 -> 2400.03.
    price = 2400.00 + 0.01 * 3
    assert execution.price_for(price, F, "SELL") == 2400.03


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


def test_stop_out_that_reduces_the_position_is_reduce_only():
    # Long +500 being cut to +100: the order opposes the position.
    assert execution.should_reduce_only(strategy.STOP_OUT, -400.0, 500.0) is True


def test_stop_out_that_increases_the_position_is_not_reduce_only():
    # The regression this gate exists for: crossing a zone boundary upward
    # lands price near the new zone's support, where the target approaches
    # max_notional, so STOP_OUT arrives as a large position-INCREASING buy.
    # Tagged reduceOnly the exchange rejects it (-2022) and, because execute()
    # raises before active_index advances, the next tick repeats it forever.
    assert execution.should_reduce_only(strategy.STOP_OUT, 575.0, 50.0) is False


def test_stop_out_increasing_a_short_is_not_reduce_only():
    assert execution.should_reduce_only(strategy.STOP_OUT, -575.0, -50.0) is False


def test_halt_flatten_is_reduce_only_in_both_directions():
    assert execution.should_reduce_only(strategy.HALT_FLATTEN, -500.0, 500.0) is True
    assert execution.should_reduce_only(strategy.HALT_FLATTEN, 500.0, -500.0) is True


def test_scale_in_is_never_reduce_only():
    assert execution.should_reduce_only(strategy.SCALE_IN, 400.0, 100.0) is False


def test_scale_out_is_never_reduce_only():
    # SCALE_OUT reduces too, but reduceOnly is reserved for the flattening
    # paths that are sized against a possibly-stale position read.
    assert execution.should_reduce_only(strategy.SCALE_OUT, -400.0, 500.0) is False


def test_reduce_only_requires_an_existing_position():
    # reduceOnly is rejected outright when there is nothing to reduce.
    assert execution.should_reduce_only(strategy.HALT_FLATTEN, -400.0, 0.0) is False


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
