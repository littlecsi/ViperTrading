import math

import pytest

import execution
import ladder
from market import Filters, MarginTier
from settings import Zone, LONG, SHORT
from strategy import distance, target_notional, signed

ZONE = Zone(support=2371.26, resistance=2625.00)
TIER = MarginTier(floor=0.0, cap=250000.0, maint_margin_rate=0.05, maint_amount=10.0)
FILTERS = Filters(step_size=0.001, min_qty=0.001, min_notional=20.0, tick_size=0.01)


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
    # Hand-derived: liq(k) = (1290 + 2310k) / (1.05 * (10 + 20k)), so
    # liq(0) = 122.857 (safe) and liq(1) = 114.286 (unsafe), and the root is
    # (126*10 - 1290) / (2310 - 126*20) = -30/-210 = 1/7.
    k = ladder.liquidation_scale(
        position_qty=10, entry_price=100, isolated_wallet=300, leverage=20,
        tier=TIER, survival_price=120, planned_delta_qty=20,
        planned_delta_notional=2200, trend=SHORT,
    )
    assert k == pytest.approx(1 / 7, rel=1e-6)


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


def test_liquidation_scale_flat_position_never_reports_an_unsafe_plan_as_safe():
    # Flat position, survival just below LiqPrice(1) = 1720/19 = 90.5263. The
    # k=0 endpoint is only "safe" via the zero-position sentinel, so the real
    # curve never crosses survival inside [0, 1]: the closed-form root lands
    # at 10.53, outside the interval. Clamping that to 1.0 would report a
    # plan as fully safe that the k=1 check just rejected.
    k = ladder.liquidation_scale(
        position_qty=0, entry_price=100, isolated_wallet=0, leverage=20,
        tier=TIER, survival_price=90.05, planned_delta_qty=20,
        planned_delta_notional=1800, trend=LONG,
    )
    assert k == 0.0


def test_survival_price_uses_next_higher_zone_for_short():
    zones = (
        Zone(support=2625.00, resistance=3284.04),
        Zone(support=2371.26, resistance=2625.00),
        Zone(support=1872.46, resistance=2371.26),
    )
    # active zone is index 2; next-higher is index 1, span 253.74.
    target = ladder.survival_price(zones, active_index=2, trend=SHORT, liquidation_buffer_pct=0.2)
    next_zone = zones[1]
    expected = next_zone.support + 0.2 * (next_zone.resistance - next_zone.support)
    assert target == pytest.approx(expected)


def test_survival_price_falls_back_to_own_span_at_the_highest_zone_for_short():
    zones = (
        Zone(support=2625.00, resistance=3284.04),
        Zone(support=2371.26, resistance=2625.00),
        Zone(support=1872.46, resistance=2371.26),
    )
    target = ladder.survival_price(zones, active_index=0, trend=SHORT, liquidation_buffer_pct=0.2)
    edge = zones[0]
    expected = edge.resistance + 0.2 * (edge.resistance - edge.support)
    assert target == pytest.approx(expected)


def test_apply_liquidation_cap_shrinks_only_the_sell_side_for_short():
    orders = ladder.desired_orders(ZONE, SHORT, max_n=5000.0, alpha=2.0, rung_spacing_pct=0.005, current_price=2500.0)
    capped = ladder.apply_liquidation_cap(orders, scale=0.5, trend=SHORT)
    for original, adjusted in zip(orders, capped):
        if original.side == "SELL":
            assert adjusted.size == pytest.approx(original.size * 0.5)
        else:
            assert adjusted.size == original.size


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
    # origQty is what execution.quantity_for would actually have produced
    # for this rung -- floored to the lot step, not the raw desired notional.
    floored_qty = execution.quantity_for(100.0, 2400.0, FILTERS)
    open_orders = [{"orderId": 1, "price": "2400.00", "side": "BUY", "origQty": str(floored_qty)}]
    plan = ladder.plan_orders(desired, open_orders, FILTERS)
    assert plan.cancel == ()
    assert plan.place == (desired[1],)


def test_plan_orders_cancels_stale_and_places_replacement():
    desired = (ladder.DesiredOrder(price=2400.0, side="BUY", size=150.0),)  # size changed
    # Open order still reflects the OLD floored quantity (was size=100.0).
    stale_qty = execution.quantity_for(100.0, 2400.0, FILTERS)
    open_orders = [{"orderId": 1, "price": "2400.00", "side": "BUY", "origQty": str(stale_qty)}]
    plan = ladder.plan_orders(desired, open_orders, FILTERS, price_tolerance=0.0001)
    assert plan.cancel == (1,)
    assert plan.place == (desired[0],)


def test_plan_orders_cancels_orders_no_longer_desired():
    desired = ()
    floored_qty = execution.quantity_for(100.0, 2400.0, FILTERS)
    open_orders = [{"orderId": 1, "price": "2400.00", "side": "BUY", "origQty": str(floored_qty)}]
    plan = ladder.plan_orders(desired, open_orders, FILTERS)
    assert plan.cancel == (1,)
    assert plan.place == ()


def test_plan_orders_leaves_alone_a_tick_rounded_match_on_a_cheap_symbol():
    """The price half of the match predicate has to respect the tick grid.

    A rung is placed at execution.price_for(raw_price, ...), so what rests on
    the book differs from the desired price by up to one full tick_size. A
    purely RELATIVE tolerance is fine while a tick is small next to it (ETH:
    tick 0.01 against a 0.24 tolerance at 2400) and inverts on a cheap symbol
    (XRP-like: tick 0.0001 against a 0.00005 tolerance at 0.50), where the
    rounding alone exceeds the tolerance and every unchanged rung is read as a
    mismatch - cancelled and re-placed on every single reconcile, which is the
    exact churn plan_orders exists to avoid."""
    cheap = Filters(step_size=0.1, min_qty=1.0, min_notional=5.0, tick_size=0.0001)
    raw_price = 0.50007
    resting_price = execution.price_for(raw_price, cheap, "BUY")
    assert resting_price == 0.5  # floored a near-full tick down
    # The rounding gap alone is bigger than the relative tolerance, which is
    # what makes this symbol different from ETHUSDT rather than just smaller.
    assert abs(raw_price - resting_price) > 0.0001 * resting_price

    desired = (ladder.DesiredOrder(price=raw_price, side="BUY", size=20.0),)
    floored_qty = execution.quantity_for(20.0, raw_price, cheap)
    open_orders = [{
        "orderId": 1, "price": str(resting_price), "side": "BUY",
        "origQty": str(floored_qty),
    }]
    plan = ladder.plan_orders(desired, open_orders, cheap)
    assert plan.cancel == ()
    assert plan.place == ()


def test_plan_orders_leaves_alone_a_lot_step_floored_match_despite_raw_notional_gap():
    # size=100.0 at price=2400.0 floors (via execution.quantity_for) to
    # qty=0.041, i.e. actual notional 98.4 -- a ~1.6% gap from the raw
    # desired notional that would blow straight through a tight relative
    # tolerance on raw notional and churn a genuinely-unchanged rung on
    # every tick. Comparing floored quantity instead correctly treats this
    # as a match: no cancel, no replacement.
    desired = (ladder.DesiredOrder(price=2400.0, side="BUY", size=100.0),)
    floored_qty = execution.quantity_for(100.0, 2400.0, FILTERS)
    assert floored_qty == pytest.approx(0.041)
    open_orders = [{"orderId": 1, "price": "2400.00", "side": "BUY", "origQty": str(floored_qty)}]
    plan = ladder.plan_orders(desired, open_orders, FILTERS)
    assert plan.cancel == ()
    assert plan.place == ()
