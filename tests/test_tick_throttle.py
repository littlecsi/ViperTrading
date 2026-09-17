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
                 price_schedule=None, position="0.0", cancel_error=None,
                 open_orders_after_tick=None, entry_price="0.0",
                 isolated_wallet="0.0", liquidation_price="0.0"):
        self.price = price
        # {ticker_price call number: new price}. bot.run() has no supported
        # pause/resume, so walking price across a zone boundary inside ONE
        # run_loop() call is the only way to drive a transition. Call 1 is
        # run()'s startup reconciliation read, so loop tick N is call N + 1.
        self.price_schedule = price_schedule or {}
        self.price_reads = 0
        self._balance = balance
        self.position = position
        # The rest of the position-risk payload. Default zeros describe a flat
        # account; a test that wants a LIVE position sets `position` together
        # with the entry price and isolated margin behind it, because the
        # liquidation cap reads all three and a non-flat position with a zero
        # entry price is not a state the exchange can report.
        self.entry_price = entry_price
        self.isolated_wallet = isolated_wallet
        self.liquidation_price = liquidation_price
        self.order_error = order_error
        self.cancel_error = cancel_error
        self.margin_type_error = margin_type_error
        self.orders = 0
        self.placed_orders = []
        # Every LIMIT order this client accepted and has not since taken off
        # the book, as the exchange would report them. {loop tick: [ids that
        # survive it]} removes rungs at a chosen tick, which is how a FILL is
        # simulated: to the bot a filled rung is simply one that stopped being
        # open.
        self.open_orders_after_tick = open_orders_after_tick or {}
        self._open_orders = []
        self.cancelled = []  # one symbol per cancel_open_orders call
        self.cancelled_order_ids = []  # one id per cancel_order call
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
            "liquidationPrice": self.liquidation_price,
            "entryPrice": self.entry_price,
            "isolatedWallet": self.isolated_wallet,
        }]

    @property
    def _tick(self):
        """Which loop tick this call falls in. get_snapshot reads ticker_price
        first thing every tick, and call 1 is run()'s startup read, so loop
        tick N is price read N + 1."""
        return self.price_reads - 1

    def get_orders(self, symbol):
        # market.get_open_orders calls the connector's plural `get_orders`
        # (GET /fapi/v1/openOrders), not the singular `get_open_order`.
        if self._tick in self.open_orders_after_tick:
            keep = self.open_orders_after_tick[self._tick]
            self._open_orders = [o for o in self._open_orders if o["orderId"] in keep]
        return list(self._open_orders)

    def cancel_order(self, symbol, orderId):
        """Per-id cancellation, for the SELECTIVE cases only - reconciling a
        settled ladder, and the liquidation backstop taking the accumulate
        side off the book while the trim side stays. The ladder TEARDOWN must
        never come through here: it abandons the whole set, and one blocking
        round-trip per rung in front of a flatten is the thing
        cancel_open_orders exists to avoid. The teardown tests therefore assert
        that this list stayed empty, which is what keeps that regression
        visible now that the method exists at all."""
        self.cancelled_order_ids.append(orderId)
        self.events.append(("cancel_one", orderId))
        self._open_orders = [o for o in self._open_orders if o["orderId"] != orderId]
        return {"orderId": orderId, "status": "CANCELED"}

    def cancel_open_orders(self, symbol):
        self.cancelled.append(symbol)
        self.events.append(("cancel", symbol))
        if self.cancel_error is not None:
            error = self.cancel_error(len(self.cancelled))
            if error:
                # A cancel that failed took nothing off the book, so the rungs
                # stay open - which is what makes the retry path testable.
                raise error
        self._open_orders = []
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
            self._open_orders.append({
                "orderId": self.orders,
                "side": params["side"],
                "price": str(params["price"]),
                "origQty": str(params["quantity"]),
            })
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


def test_the_placement_cap_reads_the_real_position_not_an_assumed_flat_one(
    monkeypatch, written
):
    """_place_ladder runs whenever the tracked set is empty, which is NOT the
    same as "the position is flat". The liquidation backstop empties the
    tracked set by cancelling the accumulate side WITHOUT closing anything, so
    the very next tick re-enters placement holding a position that is, by
    construction, close enough to liquidation for the backstop to have fired.
    Computing the cap against an assumed-flat position there would return a
    scale the real position does not justify and re-place the accumulate side
    the backstop had just taken off - cancelling and re-placing a full ladder
    every poll while the account is at its least able to afford it.

    The position here - 1.8 ETH entered at 2500 with its isolated margin
    eroded to 50 USDT, putting liquidation at ~2497 against a mark of 2500 and
    a survival target of 2271.50 - is one the cap scales to 0.0 when evaluated
    honestly. The SAME ladder against a flat position is uncapped at 1.0; see
    test_the_activation_ladder_is_capped_by_the_liquidation_guard, which uses
    the same zone, price, balance and leverage. So a BUY rung on the book
    after this tick means the cap was computed against a fiction."""
    api = LoopClient(
        price="2500.00", balance="1000.0",
        position="1.8", entry_price="2500.00", isolated_wallet="50.00",
    )
    records = run_loop(monkeypatch, written, ticks=1, api=api)

    assert "error" not in actions(records)
    sides = [o["side"] for o in api.placed_orders if o.get("type") == "LIMIT"]
    assert sides.count("BUY") == 0  # capped away by the REAL position's risk
    # The trim side reduces the very exposure that triggered the cap, so it is
    # untouched - this is a cap, not a refusal to work the zone.
    assert sides.count("SELL") > 0


def test_the_backstops_cancellation_is_not_undone_by_the_next_placement(
    monkeypatch, written
):
    """The other half of reading the real position: placement must also respect
    the exchange's REPORTED liquidation price, not only its own projection.

    The two can disagree. The projection knows the accumulate rungs sit below
    the current price, so filling them lowers the average entry and can move
    the blended liquidation to safety - it therefore answers "safe" for a
    position the exchange already reports as past its survival level. Where the
    whole ladder is accumulate-side (price near the zone's upper edge, so no
    rung sits above it), the backstop cancels every rung, the tracked set
    empties, placement runs again next tick, the projection says 1.0 and the
    full ladder goes straight back on the book - measured at 380 orders over 20
    ticks before this was fixed, one full ladder placed and cancelled per poll,
    on an account already past the level the backstop exists to defend.

    The reported price is ground truth for where the position stands NOW, so it
    overrides the projection in both directions: it stops the accumulate side
    going out at all, which is what makes the backstop's cancellation stick."""
    api = LoopClient(
        price="2600.00", balance="1000.0",
        position="1.8", entry_price="2800.00", isolated_wallet="700.00",
        # Above the zone-1 survival target of 2271.50: already breached.
        liquidation_price="2435.00",
    )
    records = run_loop(monkeypatch, written, ticks=20, api=api)

    assert "error" not in actions(records)
    sides = [o["side"] for o in api.placed_orders if o.get("type") == "LIMIT"]
    assert sides.count("BUY") == 0
    assert api.cancelled_order_ids == []  # nothing placed, so nothing to pull


def test_a_fully_blocked_placement_does_not_flood_the_tick_journal(monkeypatch, written):
    """The sustained state the fix above creates. A placement blocked in full
    sends nothing, so the tracked set stays empty, so the NEXT tick reads the
    same "no ladder yet" condition and attempts again - every poll, for as long
    as the position stays past its survival level. Attempting is not acting: a
    forced record there is the same line-per-poll flood a resting ladder used
    to produce, in the same walked-away-operator case, and the position is
    unsafe throughout it. The state is sustained, so it throttles like one."""
    api = LoopClient(
        price="2600.00", balance="1000.0",
        position="1.8", entry_price="2800.00", isolated_wallet="700.00",
        liquidation_price="2435.00",
    )
    records = run_loop(monkeypatch, written, ticks=300, api=api)

    ladder_ticks = [r for r in records if r.get("reason") == strategy.SCALE_OUT]
    assert len(ladder_ticks) == 5  # 300 polls at 1s, not 300 records
    assert api.orders == 0  # and nothing went to the exchange either


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


# --- a filled rung is detected by polling the open orders ------------------
#
# There is no fill event to subscribe to here: the bot polls its open orders
# and calls a tracked rung that has stopped being open a fill (see the design
# doc's "Risk notes" - detection is up to one poll_seconds late). Detection
# alone changes nothing on the book; it only starts the settling wait, which
# is what stops the ladder being reconciled against a position that is still
# moving.


class FakeLadderState:
    def __init__(self, orders):
        self.orders = orders


def test_detect_fill_reports_tracked_rungs_that_are_no_longer_open(monkeypatch):
    api = LoopClient()
    api._open_orders = [{"orderId": 2}, {"orderId": 3}]
    cfg = bot.settings.load()

    # Keys are the tick-ROUNDED rung prices execution.price_for produced, which
    # is what the exchange has; the values are the ids the diff works on.
    state = FakeLadderState({2499.5: 1, 2450.0: 2, 2400.0: 3})
    assert bot._detect_fill(api, cfg, state) == (1,)

    # Nothing missing, and nothing tracked at all: both are "no fill", and the
    # second must not even ask the exchange.
    assert bot._detect_fill(api, cfg, FakeLadderState({2450.0: 2, 2400.0: 3})) == ()
    api.get_orders = lambda symbol: pytest.fail("no ladder tracked: must not poll")
    assert bot._detect_fill(api, cfg, FakeLadderState({})) == ()


def test_a_filled_rung_enters_settling_without_reconciling_the_same_tick(monkeypatch, written):
    """A fill is the trigger for the settling wait, not for reconciliation.

    Reconciling on the fill tick would rebuild the ladder against a position
    that may still be filling - a fast move through several rungs would have
    the bot cancelling and replacing rungs mid-cascade, each pass priced off a
    position already out of date. So the tick that SEES the fill does nothing
    to the book; it takes a snapshot and waits for a tick that confirms the
    open-order set has stopped changing."""
    control = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=1, api=control)
    all_ids = [o["orderId"] for o in control._open_orders]
    assert len(all_ids) > 1
    filled = all_ids[0]

    api = LoopClient(
        price="2500.00", balance="1000.0",
        open_orders_after_tick={2: [i for i in all_ids if i != filled]},
    )
    run_loop(monkeypatch, written, ticks=2, api=api)

    assert filled not in [o["orderId"] for o in api._open_orders]  # it really filled
    assert api.cancelled == []  # nothing pulled off the book
    assert api.orders == control.orders  # and nothing replaced, this tick


# --- a settled ladder is reconciled against the position the fills left ----
#
# Settling ends on the first tick that finds the open-order set UNCHANGED
# since the fill that started the wait: the market has stopped moving through
# the rungs, so the ladder can be rebuilt against the position those fills
# actually left behind (design doc "Lifecycle", step 4). Reconciliation is a
# diff, not a re-place: rungs that still match are left resting.


def resting_ladder_ids(monkeypatch):
    """The rung ids one tick at 2500 rests, taken from a control run so the
    expectation tracks the real zone geometry rather than a hard-coded count."""
    control = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, [], ticks=1, api=control)
    return [o["orderId"] for o in control._open_orders]


def test_settled_ladder_reconciles_to_the_new_desired_set(monkeypatch, written):
    """One rung fills, the book then stops changing, and the gap is refilled.

    This is also the test that pins the ORDER of the two checks in run(): the
    settling branch has to be tried BEFORE _detect_fill. A filled id is absent
    from the open orders forever after, so _detect_fill keeps reporting it on
    every later tick; checked first, it would re-enter settling every tick, the
    settling branch would be unreachable, and the ladder would never be
    reconciled at all - api.orders would simply stop growing here."""
    all_ids = resting_ladder_ids(monkeypatch)
    filled = all_ids[0]

    api = LoopClient(
        price="2500.00", balance="1000.0",
        open_orders_after_tick={2: [i for i in all_ids if i != filled]},
    )
    # Tick 1 places the ladder. Tick 2 sees the rung gone -> settling. Tick 3
    # finds the open set unchanged from tick 2 -> stable -> reconcile.
    records = run_loop(monkeypatch, written, ticks=3, api=api)

    # Exactly the one missing rung is replaced: price and balance never moved,
    # so every other rung still matches what is resting and is left alone.
    assert "error" not in actions(records)  # and it got there without raising
    assert api.orders == len(all_ids) + 1
    assert api.cancelled == []  # no whole-symbol teardown
    assert api.cancelled_order_ids == []  # and nothing selectively cancelled


def test_settling_waits_while_the_open_order_set_is_still_changing(monkeypatch, written):
    """A second fill before the set stabilises restarts the wait.

    Reconciling mid-cascade would rebuild the ladder against a position that is
    still filling, once per poll, while the move is still running."""
    all_ids = resting_ladder_ids(monkeypatch)

    api = LoopClient(
        price="2500.00", balance="1000.0",
        open_orders_after_tick={
            2: [i for i in all_ids if i != all_ids[0]],
            3: [i for i in all_ids if i not in all_ids[:2]],
        },
    )
    records = run_loop(monkeypatch, written, ticks=3, api=api)

    # Tick 3 found the set changed again, so it re-snapshotted and placed
    # nothing; only tick 1's ladder has ever been sent. Checked against the
    # error stream too, so a reconcile that RAISED cannot read as one that
    # correctly declined to run.
    assert "error" not in actions(records)
    assert api.orders == len(all_ids)
    assert api.cancelled == []
    assert api.cancelled_order_ids == []


# --- the reported-liquidation-price backstop -------------------------------
#
# The cap sizes the accumulate side in ADVANCE from a projection. Binance's own
# reported liquidationPrice is the ground truth that can override it: once that
# figure has crossed the price this position has to survive to, every remaining
# accumulate-side rung comes off the book immediately, whatever the projection
# said (design doc, "Liquidation-aware buy-side cap", point 4).


def test_liquidation_breached_reads_both_trends_and_a_missing_figure():
    """The predicate shared by the two placement paths and the backstop. A LONG
    liquidates BELOW, so its reported price breaching means rising to meet the
    survival level; a SHORT liquidates ABOVE and breaches by falling to it. A
    reported 0 is 'no figure', never a breach - read literally it would put
    every SHORT in permanent breach."""
    assert bot._liquidation_breached(bot.settings.LONG, 2300.0, 2271.5) is True
    assert bot._liquidation_breached(bot.settings.LONG, 2200.0, 2271.5) is False
    assert bot._liquidation_breached(bot.settings.SHORT, 2600.0, 2728.5) is True
    assert bot._liquidation_breached(bot.settings.SHORT, 2800.0, 2728.5) is False
    assert bot._liquidation_breached(bot.settings.SHORT, 0.0, 2728.5) is False
    assert bot._liquidation_breached(bot.settings.LONG, 0.0, 2271.5) is False


class BreachedLoopClient(LoopClient):
    """A live long whose reported liquidation price crosses the zone-1 survival
    target (2371.26 - 0.2 * the next zone's span = 2271.50) partway through the
    run, which is the order events really happen in: the ladder goes on the book
    while the position is fine, and the market then moves against it.

    Breaching from the START would be a different test - the placement paths
    refuse the accumulate side outright while the reported price is breached, so
    there would be no BUY rungs on the book for the backstop to take off."""

    def get_position_risk(self, symbol=None):
        return [{
            "symbol": symbol or "ETHUSDT", "positionAmt": "1.0",
            "unRealizedProfit": "0.0",
            "liquidationPrice": "2495.00" if self._tick > 1 else "2000.00",
            "entryPrice": "2500.00", "isolatedWallet": "500.00",
        }]


def test_liquidation_backstop_cancels_remaining_accumulate_rungs(monkeypatch, written):
    api = BreachedLoopClient(price="2500.00", balance="1000.0")
    records = run_loop(monkeypatch, written, ticks=2, api=api)
    assert "error" not in actions(records)

    limits = [(i + 1, p) for i, p in enumerate(api.placed_orders) if p.get("type") == "LIMIT"]
    buy_ids = [i for i, p in limits if p["side"] == "BUY"]
    sell_ids = [i for i, p in limits if p["side"] == "SELL"]
    assert buy_ids and sell_ids  # the ladder really had both sides on it

    assert set(buy_ids).issubset(set(api.cancelled_order_ids))
    # A cap, not a teardown: the trim side reduces risk and is left resting.
    assert not set(sell_ids) & set(api.cancelled_order_ids)
    assert api.cancelled == []  # selective, so NOT cancel_open_orders


def test_liquidation_backstop_does_not_fire_while_the_position_is_safe(monkeypatch, written):
    """The default fixture reports no liquidation price and no position, which
    must read as 'nothing to guard', not as a breach."""
    api = LoopClient(price="2500.00", balance="1000.0")
    run_loop(monkeypatch, written, ticks=5, api=api)
    assert api.cancelled_order_ids == []


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
# tests therefore count cancel_open_orders calls, and assert that the fake's
# per-id cancel_order - which exists only for the selective reconcile/backstop
# cases - was never touched.


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
    assert api.cancelled_order_ids == []  # never the per-id loop


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
