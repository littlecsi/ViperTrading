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
