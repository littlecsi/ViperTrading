import math

import pytest

import ladder
from market import MarginTier
from settings import Zone, LONG, SHORT
from strategy import distance, target_notional, signed

ZONE = Zone(support=2371.26, resistance=2625.00)
TIER = MarginTier(floor=0.0, cap=250000.0, maint_margin_rate=0.05, maint_amount=10.0)


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


def test_liquidation_scale_flat_position_is_not_a_division_by_zero():
    # A round trip back to flat inside an active zone re-enters here with
    # position_qty=0. k=1 is unsafe (LiqPrice(1) = 1720/19 = 90.53 > 85), so
    # the k=0 endpoint IS evaluated -- and qty is exactly 0 there.
    k = ladder.liquidation_scale(
        position_qty=0, entry_price=100, isolated_wallet=0, leverage=20,
        tier=TIER, survival_price=85, planned_delta_qty=20,
        planned_delta_notional=1800, trend=LONG,
    )
    assert k == 0.0


def test_liquidation_scale_flat_position_still_allows_a_safe_full_plan():
    # Same flat position, loose survival target: nothing to cap.
    k = ladder.liquidation_scale(
        position_qty=0, entry_price=100, isolated_wallet=0, leverage=20,
        tier=TIER, survival_price=95, planned_delta_qty=20,
        planned_delta_notional=1800, trend=LONG,
    )
    assert k == 1.0


def test_liquidation_scale_flat_position_with_degenerate_denominator_returns_zero():
    # survival=90 makes c*delta_qty == b exactly (85.5*20 == 1710), so the
    # closed form's denominator is 0. With a flat position no k > 0 is safe.
    k = ladder.liquidation_scale(
        position_qty=0, entry_price=100, isolated_wallet=0, leverage=20,
        tier=TIER, survival_price=90, planned_delta_qty=20,
        planned_delta_notional=1800, trend=LONG,
    )
    assert k == 0.0


def test_liquidation_scale_flat_position_with_leftover_margin_finds_the_root():
    # Flat but carrying isolated wallet above maint_amount: the safe set is a
    # real interval (0, k*], and k* must land on the survival price exactly.
    k = ladder.liquidation_scale(
        position_qty=0, entry_price=100, isolated_wallet=20, leverage=20,
        tier=TIER, survival_price=85, planned_delta_qty=20,
        planned_delta_notional=1800, trend=LONG,
    )
    qty = k * 20
    wallet = 20 + k * 1800 / 20
    cost_basis = k * 1800
    liq_price = (cost_basis - wallet + TIER.maint_amount) / (qty * (1 - TIER.maint_margin_rate))
    assert liq_price == pytest.approx(85.0)
