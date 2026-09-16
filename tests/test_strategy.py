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
