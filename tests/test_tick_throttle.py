"""Tick-journal throttle.

At a one-second poll the tick journal wrote ~86400 lines a day. The throttle
suppresses only the uneventful ticks: anything that actually happened still
gets a record, because the tick log is the audit trail and the dataset a
learned policy will train on.
"""

import bot
import strategy


def drive(actions, interval=bot.TICK_LOG_INTERVAL_SECONDS):
    """Replay (action, monotonic-time) pairs through the loop's throttle the
    same way run() does, and return the actions that got journalled."""
    written = []
    last_logged_at = None
    for action, now in actions:
        if bot._should_log_tick(action, last_logged_at, now, interval):
            last_logged_at = now
            written.append((action, now))
    return written


def test_interval_is_one_minute():
    assert bot.TICK_LOG_INTERVAL_SECONDS == 60.0


def test_consecutive_holds_inside_the_window_write_once():
    written = drive([(strategy.HOLD, t) for t in range(0, 60)])
    assert written == [(strategy.HOLD, 0)]


def test_hold_after_the_window_writes_again():
    written = drive([(strategy.HOLD, t) for t in range(0, 121)])
    assert written == [(strategy.HOLD, 0), (strategy.HOLD, 60), (strategy.HOLD, 120)]


def test_idle_is_throttled_like_hold():
    written = drive([(strategy.IDLE, t) for t in (0.0, 1.0, 59.9, 60.0)])
    assert written == [(strategy.IDLE, 0.0), (strategy.IDLE, 60.0)]


def test_first_tick_always_writes():
    assert bot._should_log_tick(strategy.HOLD, None, 12345.0) is True


def test_events_inside_the_window_are_never_suppressed():
    for action in (strategy.BUY, strategy.SELL, strategy.HALT):
        written = drive([(strategy.HOLD, 0.0), (action, 1.0), (action, 1.5)])
        assert written == [(strategy.HOLD, 0.0), (action, 1.0), (action, 1.5)]


def test_event_is_written_even_immediately_after_a_throttled_hold_run():
    ticks = [(strategy.HOLD, float(t)) for t in range(0, 30)]
    ticks.append((strategy.BUY, 30.0))
    written = drive(ticks)
    assert written == [(strategy.HOLD, 0.0), (strategy.BUY, 30.0)]


def test_throttle_is_elapsed_time_not_a_tick_count():
    """Same 60s of wall clock at a 10s poll must still yield one record, so
    changing poll_seconds cannot change the journal's rate."""
    slow = drive([(strategy.HOLD, float(t)) for t in range(0, 60, 10)])
    assert slow == [(strategy.HOLD, 0.0)]
