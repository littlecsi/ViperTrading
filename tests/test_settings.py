import json
import pytest
import settings


def write(tmp_path, data):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(data))
    return str(p)


def valid_data():
    return {
        "symbol": "ETHUSDT",
        "trend": "long",
        "leverage": 5,
        "alpha": 2.0,
        "exposure_fraction": 1.0,
        "rebalance_threshold": 0.05,
        "stop_buffer": 0.01,
        "poll_seconds": 5,
        "testnet": True,
        "zones": [
            {"support": 2625.00, "resistance": 3284.04},
            {"support": 2371.26, "resistance": 2625.00},
            {"support": 1872.46, "resistance": 2371.26},
        ],
    }


def test_loads_valid_settings(tmp_path):
    s = settings.load(write(tmp_path, valid_data()))
    assert s.symbol == "ETHUSDT"
    assert s.trend == "long"
    assert s.leverage == 5
    assert len(s.zones) == 3
    assert s.zones[0].support == 2625.00
    assert s.zones[0].resistance == 3284.04


def test_rejects_unknown_trend(tmp_path):
    data = valid_data()
    data["trend"] = "sideways"
    with pytest.raises(ValueError, match="trend"):
        settings.load(write(tmp_path, data))


def test_rejects_non_positive_leverage(tmp_path):
    data = valid_data()
    data["leverage"] = 0
    with pytest.raises(ValueError, match="leverage"):
        settings.load(write(tmp_path, data))


def test_rejects_support_above_resistance(tmp_path):
    data = valid_data()
    data["zones"][0] = {"support": 3000, "resistance": 2000}
    with pytest.raises(ValueError, match="support"):
        settings.load(write(tmp_path, data))


def test_rejects_zones_not_ordered_high_to_low(tmp_path):
    data = valid_data()
    data["zones"] = [
        {"support": 1872.46, "resistance": 2371.26},
        {"support": 2625.00, "resistance": 3284.04},
    ]
    with pytest.raises(ValueError, match="descending"):
        settings.load(write(tmp_path, data))


def test_rejects_empty_zones(tmp_path):
    data = valid_data()
    data["zones"] = []
    with pytest.raises(ValueError, match="zones"):
        settings.load(write(tmp_path, data))


def test_rejects_exposure_fraction_out_of_range(tmp_path):
    data = valid_data()
    data["exposure_fraction"] = 1.5
    with pytest.raises(ValueError, match="exposure_fraction"):
        settings.load(write(tmp_path, data))
