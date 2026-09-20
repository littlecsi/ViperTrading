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


# --- reading back, for startup fill recovery -------------------------------
#
# journal.py only ever wrote until recovery needed an anchor. These readers are
# what let a restart ask "what do I already know about?" without a second
# source of truth beside the journal itself.


def test_last_order_time_is_none_when_there_is_no_journal(tmp_path):
    """A fresh install. Recovery must treat this as "no evidence", not zero -
    an anchor of 0 would sweep in the account's entire history."""
    assert journal.last_order_time(str(tmp_path / "missing")) is None
    assert journal.last_tick_time(str(tmp_path / "missing")) is None
    assert journal.known_order_ids(str(tmp_path / "missing"), since_ms=0) == set()


def test_last_order_time_prefers_the_exchange_fill_time(tmp_path):
    """A recovered row carries fill_time, the moment the exchange matched it.
    A live row has only the journal timestamp, which is later. Where the exact
    figure exists it is the better anchor."""
    journal.log_order({"order_id": 1, "fill_time": 1_700_000_000_000}, log_dir=str(tmp_path))
    assert journal.last_order_time(str(tmp_path)) == 1_700_000_000_000


def test_last_order_time_falls_back_to_the_journal_timestamp(tmp_path):
    """Live rows have no fill_time - the loop deliberately skips the
    userTrades call that would provide it."""
    journal.log_order({"order_id": 1, "side": "BUY"}, log_dir=str(tmp_path))
    at = journal.last_order_time(str(tmp_path))
    assert isinstance(at, int) and at > 1_600_000_000_000  # a plausible epoch-ms


def test_last_order_time_takes_the_maximum_across_days(tmp_path):
    """Journals are one file per day; the anchor is the newest record in any
    of them, not the newest in whichever file sorts last."""
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "orders-2026-09-18.jsonl").write_text(
        json.dumps({"order_id": 1, "fill_time": 300}) + "\n")
    (tmp_path / "orders-2026-09-19.jsonl").write_text(
        json.dumps({"order_id": 2, "fill_time": 100}) + "\n")
    assert journal.last_order_time(str(tmp_path)) == 300


def test_known_order_ids_collects_across_files(tmp_path):
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "orders-2026-09-18.jsonl").write_text(
        json.dumps({"order_id": 11, "fill_time": 100}) + "\n")
    (tmp_path / "orders-2026-09-19.jsonl").write_text(
        json.dumps({"order_id": 22, "fill_time": 200}) + "\n"
        + json.dumps({"order_id": 33, "fill_time": 300}) + "\n")
    assert journal.known_order_ids(str(tmp_path), since_ms=0) == {11, 22, 33}


def test_known_order_ids_ignores_rows_with_no_order_id(tmp_path):
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "orders-2026-09-19.jsonl").write_text(
        json.dumps({"side": "BUY"}) + "\n" + json.dumps({"order_id": 7}) + "\n")
    assert journal.known_order_ids(str(tmp_path), since_ms=0) == {7}


def test_a_malformed_line_does_not_abort_the_read(tmp_path):
    """A truncated final line is what a journal written by a process that was
    killed mid-write looks like - exactly the situation recovery runs in. One
    unreadable line must not cost every readable one around it."""
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "orders-2026-09-19.jsonl").write_text(
        json.dumps({"order_id": 1, "fill_time": 100}) + "\n"
        + '{"order_id": 2, "fill_ti\n'          # truncated mid-write
        + json.dumps({"order_id": 3, "fill_time": 300}) + "\n")

    assert journal.known_order_ids(str(tmp_path), since_ms=0) == {1, 3}
    assert journal.last_order_time(str(tmp_path)) == 300


def test_last_tick_time_reads_the_tick_journal(tmp_path):
    """The cold-start anchor: no order journal, but the tick journal still
    says when this bot was last provably alive."""
    journal.log_tick({"action": "hold"}, log_dir=str(tmp_path))
    at = journal.last_tick_time(str(tmp_path))
    assert isinstance(at, int) and at > 1_600_000_000_000
