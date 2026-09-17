import math

import pytest

import ladder
from settings import Zone, LONG, SHORT
from strategy import distance, target_notional, signed

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
