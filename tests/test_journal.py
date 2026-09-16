import json
from pathlib import Path

import journal


def test_log_tick_appends_jsonl(tmp_path):
    journal.log_tick({"price": 2403.0, "action": "buy"}, log_dir=str(tmp_path))
    journal.log_tick({"price": 2404.0, "action": "hold"}, log_dir=str(tmp_path))

    files = list(Path(tmp_path).glob("ticks-*.jsonl"))
    assert len(files) == 1

    lines = files[0].read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["price"] == 2403.0
    assert json.loads(lines[1])["action"] == "hold"


def test_log_tick_adds_timestamp(tmp_path):
    journal.log_tick({"action": "hold"}, log_dir=str(tmp_path))
    record = json.loads(list(Path(tmp_path).glob("ticks-*.jsonl"))[0].read_text())
    assert "timestamp" in record


def test_log_order_writes_separate_file(tmp_path):
    journal.log_order({"side": "BUY", "support": 2371.26}, log_dir=str(tmp_path))

    orders = list(Path(tmp_path).glob("orders-*.jsonl"))
    assert len(orders) == 1
    assert list(Path(tmp_path).glob("ticks-*.jsonl")) == []

    record = json.loads(orders[0].read_text())
    assert record["side"] == "BUY"
    assert record["support"] == 2371.26


def test_creates_log_dir_if_missing(tmp_path):
    target = tmp_path / "nested" / "logs"
    journal.log_tick({"action": "hold"}, log_dir=str(target))
    assert target.exists()
