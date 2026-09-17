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

    def __init__(self, price="2700.00", balance="0.0", order_error=None, margin_type_error=None,
                 price_schedule=None, position="0.0", cancel_error=None):
        self.price = price
        # {ticker_price call number: new price}. bot.run() has no supported
        # pause/resume, so walking price across a zone boundary inside ONE
        # run_loop() call is the only way to drive a transition. Call 1 is
        # run()'s startup reconciliation read, so loop tick N is call N + 1.
        self.price_schedule = price_schedule or {}
        self.price_reads = 0
        self._balance = balance
        self.position = position
        self.order_error = order_error
        self.cancel_error = cancel_error
        self.margin_type_error = margin_type_error
        self.orders = 0
        self.placed_orders = []
        self.cancelled = []  # one symbol per cancel_open_orders call
        self.events = []  # ("place", type) / ("cancel", symbol), in call order
        self.margin_type_calls = []

    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "20"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    ],
                }
                # BTCUSDT is here only so a symbol-switch reload has somewhere
                # to switch TO; the filters are deliberately identical.
                for symbol in ("ETHUSDT", "BTCUSDT")
            ]
        }

    def change_leverage(self, symbol, leverage):
        return {"symbol": symbol, "leverage": leverage}

    def ticker_price(self, symbol):
        self.price_reads += 1
        if self.price_reads in self.price_schedule:
            self.price = self.price_schedule[self.price_reads]
        return {"symbol": symbol, "price": self.price}

    def balance(self):
        return [{"asset": "USDT", "balance": self._balance, "availableBalance": self._balance}]

    def get_position_risk(self, symbol=None):
        return [{
            "symbol": symbol or "ETHUSDT", "positionAmt": self.position,
            "unRealizedProfit": "0.0",
            "liquidationPrice": "0.0", "entryPrice": "0.0", "isolatedWallet": "0.0",
        }]

    def get_orders(self, symbol):
        # market.get_open_orders calls the connector's plural `get_orders`
        # (GET /fapi/v1/openOrders), not the singular `get_open_order`.
        return []

    # Deliberately NO cancel_order: the ladder teardown must take the whole
    # symbol off the book in one request, so a regression to the per-id loop
    # cannot quietly pass these tests - it raises AttributeError instead.
    def cancel_open_orders(self, symbol):
        self.cancelled.append(symbol)
        self.events.append(("cancel", symbol))
        if self.cancel_error is not None:
            error = self.cancel_error(len(self.cancelled))
            if error:
                raise error
        return {"code": 200, "msg": "The operation of cancel all open order is done."}

    def new_order(self, **params):
        self.orders += 1
        self.placed_orders.append(params)
        self.events.append(("place", params.get("type")))
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


def test_ladder_orders_are_placed_at_tick_rounded_prices(monkeypatch, written):
    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=api)
    for order in api.placed_orders:
        if order.get("type") == "LIMIT":
            # ETHUSDT tick size in this fixture is 0.01 -- every price must
            # be an exact multiple, not a raw theoretical rung price.
            assert round(order["price"] / 0.01) == pytest.approx(order["price"] / 0.01, abs=1e-6)


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


def test_the_activation_ladder_is_capped_by_the_liquidation_guard(monkeypatch, written):
    """The activation ladder is the largest burst of accumulation the bot ever
    commits to - every rung in the zone at once - so it is capped like any
    other. At 5x the whole ladder still survives down to the next zone and
    every accumulate rung is placed. At 10x the projected liquidation sits the
    wrong side of that level, so the cap takes the accumulate side to nothing
    while leaving the trim side - which reduces risk - alone."""
    safe = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=safe)
    safe_sides = [o["side"] for o in safe.placed_orders]
    assert safe_sides.count("BUY") > 0  # uncapped: the accumulate side goes out

    risky_cfg = bot.settings.Settings(**{**bot.settings.load().__dict__, "leverage": 10})
    risky = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, [], ticks=1, api=risky, load=lambda: risky_cfg)
    risky_sides = [o["side"] for o in risky.placed_orders]
    assert risky_sides.count("BUY") == 0  # capped away entirely
    # Not simply "nothing was placed": the trim side is untouched by the cap,
    # which is what makes this a cap and not a refusal to trade the zone.
    assert risky_sides.count("SELL") > 0


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


# --- the ladder comes off the book when its zone is left -------------------
#
# A resting rung is a live order at a price chosen for ONE zone. Every way of
# leaving that zone - stepping to the next one, stopping out of it, halting off
# the ladder entirely, or switching symbol - has to take those rungs off the
# book. Forgetting them (resetting the tracked ids without cancelling) leaves
# orders nothing supervises, which on the accumulate side can re-open a
# position the bot has just decided to be out of.
#
# The teardown is ONE request for the whole symbol, never one per rung: these
# tests therefore count cancel_open_orders calls, and the fake deliberately has
# no cancel_order at all.


def ladder_order_ids(api):
    """Ids of the LIMIT orders this client was asked to place, in order. The
    fake numbers order ids from 1 in call order, so the index gives the id."""
    return [i + 1 for i, p in enumerate(api.placed_orders) if p.get("type") == "LIMIT"]


def first_tick_ladder(monkeypatch, position="0.0"):
    """The rung ids one tick at 2500 places - the ladder a later tick has to
    cancel. Taken from a control run rather than assumed, so the expectation
    tracks the real zone geometry."""
    control = LoopClient(price="2500.00", balance="1000.0", position=position)
    run_loop(monkeypatch, [], ticks=1, api=control)
    return ladder_order_ids(control)


def test_a_zone_change_cancels_the_old_zones_ladder(monkeypatch, written):
    """Zone 1 (2371.26-2625.00) activates at 2500 and rests a ladder. Price
    then falls to 2200, past the stop_buffer below that support, so zone 2
    takes over: the old zone's rungs are priced for a zone the bot has left and
    must be cancelled, not merely forgotten, before the new ladder goes on."""
    api = LoopClient(price="2500.00", balance="1000.0", price_schedule={3: "2200.00"})
    run_loop(monkeypatch, written, ticks=2, api=api)

    first = first_tick_ladder(monkeypatch)
    assert first  # the first tick really did rest a ladder
    assert api.cancelled == ["ETHUSDT"]
    assert len(ladder_order_ids(api)) > len(first)  # and a fresh one replaced it


def test_the_ladder_is_torn_down_in_one_request_not_one_per_rung(monkeypatch, written):
    """Cancelling id by id is one blocking round-trip per rung - 19 of them for
    this repo's own zone geometry - all of which the exit path has to finish
    before it can flatten. That burst is also exactly what gets an IP rate
    limited, and being rate limited while holding a leveraged position the bot
    then cannot flatten is the worst outcome in the system. The teardown is one
    DELETE /fapi/v1/allOpenOrders for the whole symbol, whatever the rung
    count."""
    api = LoopClient(price="2500.00", balance="1000.0", price_schedule={3: "2200.00"})
    run_loop(monkeypatch, written, ticks=2, api=api)

    rungs = len(first_tick_ladder(monkeypatch))
    assert rungs > 1  # there really were several rungs to take off
    assert len(api.cancelled) == 1  # and exactly one request took them all


def test_a_zone_change_decided_as_hold_still_cancels(monkeypatch, written):
    """The same zone change, at a price where the new zone's target lands
    inside the rebalance threshold: the strategy says HOLD, and a HOLD normally
    leaves a resting ladder alone. The zone still changed, though, so the old
    rungs have to go - otherwise the next tick sees the zone as unchanged with
    orders already tracked and adopts zone 1's ladder as zone 2's."""
    api = LoopClient(price="2500.00", balance="1000.0", price_schedule={3: "2300.00"})
    records = run_loop(monkeypatch, written, ticks=2, api=api)

    assert records[-1]["action"] == strategy.HOLD  # not the ladder branch
    assert records[-1]["active_zone_index"] == 2  # but a different zone
    assert api.cancelled == ["ETHUSDT"]


def test_stop_out_cancels_the_resting_ladder_before_the_market_order(monkeypatch, written):
    """Same zone change, but holding a position, so the strategy calls it a
    STOP_OUT and exits with a MARKET order. The rungs must come off the book
    BEFORE that order: an accumulate rung left resting can fill straight back
    into the position the stop-out just closed."""
    api = LoopClient(
        price="2500.00", balance="1000.0", position="0.2",
        price_schedule={3: "2300.00"},
    )
    run_loop(monkeypatch, written, ticks=2, api=api)

    assert api.cancelled == ["ETHUSDT"]
    kinds = [kind for kind, _ in api.events]
    assert ("place", "MARKET") in api.events  # the exit itself still crosses
    market_at = api.events.index(("place", "MARKET"))
    assert kinds.index("cancel") < market_at
    assert "cancel" not in kinds[market_at:]  # all of it, before the exit


def test_halt_cancels_the_resting_ladder_before_flattening(monkeypatch, written):
    """Price leaves the ladder below every support: HALT_FLATTEN. The flatten
    is worthless if a BUY rung is still resting underneath it."""
    api = LoopClient(
        price="2500.00", balance="1000.0", position="0.2",
        price_schedule={3: "1800.00"},
    )
    records = run_loop(monkeypatch, written, ticks=2, api=api)

    assert any(r.get("action") == strategy.HALT for r in records)
    assert api.cancelled == ["ETHUSDT"]
    kinds = [kind for kind, _ in api.events]
    assert kinds.index("cancel") < api.events.index(("place", "MARKET"))


def test_a_failing_cancel_never_gates_the_halt_flatten(monkeypatch, written):
    """The one thing the exit path may never do is fail to flatten.

    A cancel that keeps raising is not exotic here: rate limiting or a network
    fault is *correlated* with whatever move triggered the HALT. Gated on the
    cancel, the tick aborted before the market order, `halted` had already
    latched, and the bot sat on a leveraged position indefinitely with
    notify.halt never called even once. The cancel is best-effort: it is
    attempted first, its failure is journalled like any other loop error, and
    the flatten goes out regardless.

    Journalled and NOTIFIED are not the same step here. The journal write is a
    local append and happens immediately; the Telegram push is an HTTP call
    worth up to notify.TIMEOUT_SECONDS, so it waits until the market order has
    actually gone out - the same ordering the FLATTEN_FAILED notification
    already follows. It still has to happen, just not first."""
    from binance.error import ClientError

    halts = []
    monkeypatch.setattr(bot.notify, "halt", lambda **kw: halts.append(kw.get("outcome")))

    api = LoopClient(
        price="2500.00", balance="1000.0", position="0.2",
        price_schedule={3: "1800.00"},
        cancel_error=lambda n: ClientError(429, -1003, "Too many requests", {}),
    )
    # Recorded into the same timeline as the exchange calls, which is the only
    # way to see WHERE the push happened relative to the market order.
    monkeypatch.setattr(
        bot.notify, "loop_error",
        lambda message, symbol=None: api.events.append(("notify", message)),
    )
    records = run_loop(monkeypatch, written, ticks=2, api=api)

    assert ("place", "MARKET") in api.events  # flattened on THIS tick
    assert halts == [bot.notify.FLATTENED]  # and said so
    errors = [r["error"] for r in records if r.get("action") == "error"]
    assert any("ladder cancel failed" in e for e in errors)  # not swallowed

    pushed = [i for i, (kind, text) in enumerate(api.events)
              if kind == "notify" and "ladder cancel failed" in text]
    assert pushed  # and still pushed, not just written to disk
    assert api.events.index(("place", "MARKET")) < pushed[0]  # after the exit


def test_a_symbol_switch_cancels_the_old_symbols_ladder(monkeypatch, written):
    """Switching symbol resets the zone state, and the old symbol's rungs have
    to go with it - against the OLD symbol, which is where they are resting."""
    base = bot.settings.load()
    switched = bot.settings.Settings(**{**base.__dict__, "symbol": "BTCUSDT"})
    calls = {"n": 0}

    def load():
        calls["n"] += 1
        # Tick 1 sees the running config (the ladder goes on ETHUSDT); the
        # switch arrives on tick 2.
        return base if calls["n"] == 1 else switched

    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=2, api=api, load=load)

    # Against the OLD symbol: that is where the rungs are actually resting.
    assert api.cancelled == ["ETHUSDT"]


def test_a_failed_cancel_is_retried_rather_than_stranding_the_old_rungs(monkeypatch, written):
    """A cancel that fails has placed nothing, so the zone change must stay
    unfinished and be retried. Advancing past it would leave the old zone's
    rungs live on the book while the bot counted them as the new zone's."""
    from binance.error import ClientError

    api = LoopClient(
        price="2500.00", balance="1000.0", price_schedule={3: "2200.00"},
        cancel_error=lambda n: ClientError(400, -1001, "Internal error", {}) if n == 1 else None,
    )
    records = run_loop(monkeypatch, written, ticks=3, api=api)

    first = first_tick_ladder(monkeypatch)
    assert actions(records)["error"] == 1  # the failure was recorded, not hidden
    # Tried on the tick it failed, and again on the next one until it took.
    assert api.cancelled == ["ETHUSDT", "ETHUSDT"]
    assert len(ladder_order_ids(api)) > len(first)  # and the new zone's ladder went on
