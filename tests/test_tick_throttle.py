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


# --- the whole loop: sustained failures must not flood --------------------
#
# These drive the real bot.run() so the wiring is covered, not just the policy
# object: a TickLog that is correct but wired to the wrong stream would pass
# every test above and still flood the journal.


class LoopClient:
    """Enough of UMFutures for one trip round bot.run()."""

    def __init__(self, price="2700.00", balance="0.0", order_error=None, margin_type_error=None):
        self.price = price
        self._balance = balance
        self.order_error = order_error
        self.margin_type_error = margin_type_error
        self.orders = 0
        self.placed_orders = []
        self.margin_type_calls = []

    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "ETHUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "20"},
                    ],
                }
            ]
        }

    def change_leverage(self, symbol, leverage):
        return {"symbol": symbol, "leverage": leverage}

    def ticker_price(self, symbol):
        return {"symbol": symbol, "price": self.price}

    def balance(self):
        return [{"asset": "USDT", "balance": self._balance, "availableBalance": self._balance}]

    def get_position_risk(self, symbol=None):
        return [{
            "symbol": "ETHUSDT", "positionAmt": "0.0", "unRealizedProfit": "0.0",
            "liquidationPrice": "0.0", "entryPrice": "0.0", "isolatedWallet": "0.0",
        }]

    def get_orders(self, symbol):
        # market.get_open_orders calls the connector's plural `get_orders`
        # (GET /fapi/v1/openOrders), not the singular `get_open_order`.
        return []

    def new_order(self, **params):
        self.orders += 1
        self.placed_orders.append(params)
        if self.order_error is not None:
            # A falsy return means "accept this one", so a test can reject only
            # some calls - a ladder whose margin runs out partway through.
            message = self.order_error(self.orders)
            if message:
                raise RuntimeError(message)
        if params.get("type") == "LIMIT":
            return {"orderId": self.orders, "status": "NEW"}
        return {"orderId": self.orders, "avgPrice": "0", "executedQty": "0", "cumQuote": "0"}

    def change_margin_type(self, symbol, marginType):
        self.margin_type_calls.append((symbol, marginType))
        if self.margin_type_error is not None:
            raise self.margin_type_error

    def leverage_brackets(self, symbol):
        return [{"symbol": symbol, "brackets": [
            {"bracket": 1, "initialLeverage": 20, "notionalCap": 50000.0,
             "notionalFloor": 0.0, "maintMarginRatio": 0.01, "cum": 0.0},
        ]}]


def run_loop(monkeypatch, written, ticks, api=None, load=None):
    """Run bot.run() for `ticks` polls on a fake clock, returning the records."""
    api = api or LoopClient()
    clock = Clock()
    real_ticklog = bot.TickLog

    monkeypatch.setattr(bot.client, "build", lambda testnet: api)
    monkeypatch.setattr(bot, "TickLog", lambda *a, **kw: real_ticklog(clock=clock))
    monkeypatch.setattr(journal, "log_order", lambda record, log_dir=None: None)

    if load is not None:
        real_load = bot.settings.load
        calls = {"n": 0}

        def patched(*a, **kw):
            calls["n"] += 1
            if calls["n"] > 1:  # the first call is startup, before the loop
                return load()
            return real_load(*a, **kw)

        monkeypatch.setattr(bot.settings, "load", patched)

    polls = {"n": 0}

    def sleep(seconds):
        polls["n"] += 1
        if polls["n"] >= ticks:
            raise KeyboardInterrupt
        clock.advance(1.0)  # poll_seconds is 1

    monkeypatch.setattr(bot, "_sleep", sleep)
    try:
        # run() catches KeyboardInterrupt only inside the per-tick try; the
        # sleep that ends a tick which placed an order sits outside it.
        bot.run()
    except KeyboardInterrupt:
        pass
    assert polls["n"] == ticks
    return written


def actions(records):
    counts = {}
    for r in records:
        counts[r.get("action")] = counts.get(r.get("action"), 0) + 1
    return counts


def test_type_error_from_settings_is_a_config_error_not_a_loop_error(monkeypatch, written):
    """'leverage': null reaches int(None). If TypeError escapes the reload
    handler it becomes an unthrottled loop error - one record per poll."""

    def broken():
        raise TypeError("int() argument must be a string or a number, not 'NoneType'")

    records = run_loop(monkeypatch, written, ticks=300, load=broken)
    counts = actions(records)
    assert counts["config_error"] == 5  # 299s of polling, not 300 records
    assert "error" not in counts  # never reached the loop's generic handler


def test_repeated_order_rejection_collapses(monkeypatch, written):
    """-2019 repeats on every poll once required margin exceeds the wallet."""
    api = LoopClient(balance="1000.0", order_error=lambda n: "-2019 Margin is insufficient")
    records = run_loop(monkeypatch, written, ticks=300, api=api)

    assert api.orders == 300  # the bot kept trying, as it should
    assert actions(records)["error"] == 5


def test_leverage_brackets_are_fetched_once_at_startup(monkeypatch, written):
    api = LoopClient()
    api.leverage_bracket_calls = []

    def leverage_brackets(symbol):
        api.leverage_bracket_calls.append(symbol)
        return [{"symbol": symbol, "brackets": [
            {"bracket": 1, "initialLeverage": 20, "notionalCap": 50000.0,
             "notionalFloor": 0.0, "maintMarginRatio": 0.01, "cum": 0.0},
        ]}]

    monkeypatch.setattr(api, "leverage_brackets", leverage_brackets, raising=False)
    run_loop(monkeypatch, written, ticks=5, api=api)
    assert api.leverage_bracket_calls == ["ETHUSDT"]  # once, not once per tick


def test_a_different_error_is_written_immediately(monkeypatch, written):
    api = LoopClient(
        balance="1000.0",
        order_error=lambda n: "-2019 Margin is insufficient" if n <= 90 else "-1021 Timestamp",
    )
    records = run_loop(monkeypatch, written, ticks=95, api=api)

    errors = [r["error"] for r in records if r.get("action") == "error"]
    assert errors == [
        "-2019 Margin is insufficient",  # first failure
        "-2019 Margin is insufficient",  # 60s later
        "-1021 Timestamp",  # the tick it changed, not 60s after
    ]


# --- ISOLATED margin at startup --------------------------------------------


def test_startup_sets_isolated_margin(monkeypatch, written):
    api = LoopClient()
    run_loop(monkeypatch, written, ticks=1, api=api)
    assert api.margin_type_calls == [("ETHUSDT", "ISOLATED")]


def test_startup_tolerates_already_isolated(monkeypatch, written):
    from binance.error import ClientError

    api = LoopClient(margin_type_error=ClientError(400, -4046, "No need to change margin type.", {}))
    # Must not raise / must not prevent the loop from running.
    run_loop(monkeypatch, written, ticks=1, api=api)


def test_startup_warns_but_continues_when_position_open(monkeypatch, written, capsys):
    from binance.error import ClientError

    api = LoopClient(
        margin_type_error=ClientError(
            400, -4047, "Margin type cannot be changed if there exists position.", {}
        )
    )
    run_loop(monkeypatch, written, ticks=1, api=api)
    output = capsys.readouterr().out
    assert "ISOLATED" in output.upper()


# --- in-zone SCALE_IN/SCALE_OUT goes to the limit-order ladder -------------


def test_zone_activation_places_a_full_ladder_of_limit_orders(monkeypatch, written):
    # Price sits inside the middle of the configured zone ladder, trend long,
    # zero starting position -> zone activates and the ladder places.
    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=api)
    assert api.orders > 0
    assert all(o["type"] == "LIMIT" for o in api.placed_orders)


def test_a_resting_ladder_is_placed_once_not_re_placed_every_tick(monkeypatch, written):
    """Price sits still inside the zone: the ladder goes on the book once and
    is then left alone, however long price stays there."""
    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=300, api=api)

    after_one_tick = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, [], ticks=1, api=after_one_tick)
    assert api.orders == after_one_tick.orders > 0  # 300 polls, one placement


def test_a_resting_ladder_does_not_flood_the_tick_journal(monkeypatch, written):
    """The position stays flat while the rungs rest, so `delta` - and with it
    the old forced=sending - stays large on every poll. Forcing on that wrote a
    line per poll for as long as price sat in the zone, which is the flood
    TickLog exists to prevent. A resting ladder is a sustained state and
    throttles like one; the tick that PLACED it is a transition and is kept."""
    api = LoopClient(price="2500.00", balance="1000.0")
    records = run_loop(monkeypatch, written, ticks=300, api=api)

    ladder_ticks = [r for r in records if r.get("reason") == strategy.SCALE_IN]
    assert len(ladder_ticks) == 5  # 300s at a 1s poll, not 300 records


def test_a_partly_rejected_ladder_is_not_re_placed_every_tick(monkeypatch, written):
    """The rungs accepted before a rejection are LIVE on the exchange. If
    active_index did not advance past a placement that raised, the next tick
    would read the zone as still changed, forget those live orders and place
    the whole set again - once per poll, unbounded. Rejection partway through is
    ordinary: at exposure_fraction 1.0 the margin runs out mid-ladder (-2019)."""
    api = LoopClient(
        price="2500.00", balance="1000.0",
        # Accept four rungs, then reject, exactly as a margin wall would.
        order_error=lambda n: "-2019 Margin is insufficient" if n % 5 == 0 else None,
    )
    run_loop(monkeypatch, written, ticks=10, api=api)

    assert api.orders == 5  # four placed and the one rejected, then it stops
    # The four live rungs are still tracked, so no tick re-places them.
    assert len(api.placed_orders) == 5


def test_a_fully_rejected_ladder_still_retries_next_tick(monkeypatch, written):
    """The other half of the partial-failure rule: when NOTHING was accepted
    there is nothing resting to duplicate, so the tracked set stays empty and
    the next tick must retry from scratch rather than give up on the zone."""
    api = LoopClient(
        price="2500.00", balance="1000.0",
        order_error=lambda n: "-2019 Margin is insufficient",
    )
    run_loop(monkeypatch, written, ticks=10, api=api)

    assert api.orders == 10  # one attempt per tick, still trying
