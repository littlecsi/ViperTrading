"""Tick-journal write policy (bot.TickLog).

At a one-second poll the unthrottled tick journal wrote ~86400 lines a day.
The throttle collapses a REPEATED state to one line per interval; it must never
lose a transition, an order, or a rare one-off record. Throttling on the action
label instead would flood the log for every sticky state (HALT once price leaves
the ladder, an unexecutable SCALE_OUT on residual dust, config_error on an
invalid settings.json) - which is the failure this module is built to avoid.

These drive the real bot.TickLog and capture what reaches journal.log_tick, so
the timestamp/key bookkeeping under test is the code bot.py runs, not a
re-implementation of it.
"""

import pytest

import bot
import journal
import strategy


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def written(monkeypatch):
    records = []
    monkeypatch.setattr(journal, "log_tick", lambda record, log_dir=None: records.append(record))
    return records


@pytest.fixture
def clock():
    return Clock()


def tick_log(clock):
    return bot.TickLog(clock=clock)


def decision_record(action, reason=None):
    return {"action": action, "reason": reason}


def poll(log, clock, action, reason=None, forced=False, ticks=1, seconds=1.0):
    """Replay `ticks` polls of one unchanging state through the write policy."""
    for _ in range(ticks):
        log.log(decision_record(action, reason), key=(action, reason), forced=forced)
        clock.advance(seconds)


def test_interval_is_one_minute():
    assert bot.TICK_LOG_INTERVAL_SECONDS == 60.0


def test_first_record_is_always_written(written, clock):
    assert tick_log(clock).log(decision_record(strategy.HOLD), key=(strategy.HOLD, None)) is True
    assert len(written) == 1


# --- sustained states collapse --------------------------------------------


def test_repeated_hold_collapses_to_one_per_interval(written, clock):
    poll(tick_log(clock), clock, strategy.HOLD, ticks=300)  # 300s at a 1s poll
    assert len(written) == 5


def test_sustained_halt_does_not_flood(written, clock):
    """Once price leaves the ladder every tick decides HALT. Label-based
    throttling wrote one line per poll for as long as that lasted."""
    poll(tick_log(clock), clock, strategy.HALT, "halt_flatten", ticks=300)
    assert len(written) == 5
    assert all(r["action"] == strategy.HALT for r in written)


def test_repeated_unexecutable_scale_out_does_not_flood(written, clock):
    """Residual dust below min_notional: decide() returns SCALE_OUT forever and
    is_executable rejects it every time, so no order is ever sent."""
    poll(tick_log(clock), clock, strategy.SELL, strategy.SCALE_OUT, forced=False, ticks=300)
    assert len(written) == 5


def test_repeated_config_error_does_not_flood(written, clock):
    log = tick_log(clock)
    for _ in range(300):
        log.log({"action": "config_error", "error": "bad json"}, key=("config_error", "bad json"))
        clock.advance(1.0)
    assert len(written) == 5


def test_a_different_config_error_is_written_immediately(written, clock):
    log = tick_log(clock)
    log.log({"action": "config_error", "error": "bad json"}, key=("config_error", "bad json"))
    clock.advance(1.0)
    log.log({"action": "config_error", "error": "leverage"}, key=("config_error", "leverage"))
    assert [r["error"] for r in written] == ["bad json", "leverage"]


def test_interleaved_streams_need_separate_logs(written, clock):
    """Why bot.py gives config_error its own TickLog: through one log the two
    record kinds alternate, each looks like a change, and neither throttles."""
    shared = tick_log(clock)
    for _ in range(10):
        shared.log({"action": "config_error"}, key=("config_error", "bad json"))
        shared.log(decision_record(strategy.HOLD), key=(strategy.HOLD, None))
        clock.advance(1.0)
    assert len(written) == 20  # the flood that separate logs prevent

    written.clear()
    ticks, configs = tick_log(clock), tick_log(clock)
    for _ in range(10):
        configs.log({"action": "config_error"}, key=("config_error", "bad json"))
        ticks.log(decision_record(strategy.HOLD), key=(strategy.HOLD, None))
        clock.advance(1.0)
    assert len(written) == 2  # one of each, then both throttled


# --- transitions and events are never lost --------------------------------


def test_hold_to_halt_is_written_immediately(written, clock):
    log = tick_log(clock)
    poll(log, clock, strategy.HOLD, ticks=30)
    poll(log, clock, strategy.HALT, "halt_flatten", ticks=1)
    assert [r["action"] for r in written] == [strategy.HOLD, strategy.HALT]


def test_halt_to_hold_is_written_immediately(written, clock):
    log = tick_log(clock)
    poll(log, clock, strategy.HALT, "halt_flatten", ticks=30)
    poll(log, clock, strategy.HOLD, ticks=1)
    assert [r["action"] for r in written] == [strategy.HALT, strategy.HOLD]


def test_reason_change_within_one_action_is_written(written, clock):
    log = tick_log(clock)
    poll(log, clock, strategy.SELL, strategy.SCALE_OUT, ticks=10)
    poll(log, clock, strategy.SELL, strategy.STOP_OUT, ticks=1)
    assert [r["reason"] for r in written] == [strategy.SCALE_OUT, strategy.STOP_OUT]


def test_buy_inside_a_throttled_run_is_never_suppressed(written, clock):
    log = tick_log(clock)
    poll(log, clock, strategy.HOLD, ticks=5)
    poll(log, clock, strategy.BUY, strategy.SCALE_IN, forced=True, ticks=1)
    assert [r["action"] for r in written] == [strategy.HOLD, strategy.BUY]


def test_every_sent_order_is_written_even_repeating_the_same_state(written, clock):
    """forced=True is 'an order is going out this tick'. Repeated identical
    scale-ins one second apart must each leave a record."""
    poll(tick_log(clock), clock, strategy.BUY, strategy.SCALE_IN, forced=True, ticks=10)
    assert len(written) == 10


def test_throttled_repeat_reports_false_and_writes_nothing(written, clock):
    log = tick_log(clock)
    assert log.log(decision_record(strategy.HOLD), key=(strategy.HOLD, None)) is True
    clock.advance(1.0)
    assert log.log(decision_record(strategy.HOLD), key=(strategy.HOLD, None)) is False
    assert len(written) == 1


def test_throttle_is_elapsed_time_not_a_tick_count(written, clock):
    """Same 60s of wall clock at a 10s poll: still one record, so changing
    poll_seconds cannot change the journal's rate."""
    poll(tick_log(clock), clock, strategy.HOLD, ticks=6, seconds=10.0)
    assert len(written) == 1


# --- config_reloaded -------------------------------------------------------


def test_config_changes_reports_only_what_changed():
    old = bot.settings.load()
    new = bot.settings.Settings(**{**old.__dict__, "leverage": old.leverage + 1})
    assert bot._config_changes(old, new) == {"leverage": [old.leverage, old.leverage + 1]}


def test_config_changes_is_json_safe():
    import json

    old = bot.settings.load()
    new = bot.settings.Settings(**{**old.__dict__, "zones": old.zones[:1], "trend": "short"})
    changes = bot._config_changes(old, new)
    assert set(changes) == {"zones", "trend"}
    json.dumps(changes)  # zones must not reach the journal as dataclasses


def test_config_reloaded_record_is_written_unthrottled(written):
    cfg = bot.settings.load()
    for _ in range(3):
        bot._log_config_reloaded(cfg, {"leverage": [5, 10]})
    assert len(written) == 3
    assert written[0]["action"] == "config_reloaded"
    assert written[0]["changed"] == {"leverage": [5, 10]}
