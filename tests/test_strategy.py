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
