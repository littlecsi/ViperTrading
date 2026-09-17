"""Telegram notifications (futures/notify.py).

Two things are being protected here, and neither of them is the wording.

The first is that a notification can never hurt a trade. This bot has been
killed once already by an exception raised inside an exception handler while it
held a leveraged position, so every path out of notify - a dead network, a
localised OSError the cp949 console cannot print, a formatter handed a record
missing half its fields, a send() that raises outright - has to end in a return
value rather than a traceback, and bot.run() has to come through it with the
order placed and journalled.

The second is that it cannot spam. Several of the loop's states are sticky:
once price leaves the ladder every tick decides HALT, and a refused settings
file is re-read and re-refused every poll. Those notify on the TRANSITION;
orders, which are rare and move real money, notify every single time.

Nothing here touches the network: requests.post is replaced in every test, and
the credentials are fakes.
"""

import json
from pathlib import Path

import pytest

import bot
import journal
import notify
import test_tick_throttle as loop  # LoopClient/run_loop: the real bot.run()

TOKEN = "1234567890:FAKE-TOKEN-FOR-TESTS"
CHAT_ID = 999

# Korean for "connection failed", the shape of message a localised Windows
# delivers to this machine. Built from code points so that this file itself
# stays ASCII - the repository is checked for non-ASCII bytes.
LOCALISED_ERROR = "".join(chr(c) for c in (0xC5F0, 0xACB0, 0x20, 0xC2E4, 0xD328))


class FakeConfig:
    def __init__(self, token=TOKEN, chat_id=CHAT_ID):
        if token is not None:
            self.telegram_token = token
        if chat_id is not None:
            self.telegram_chat_id = chat_id


class Response:
    def __init__(self, status_code=200):
        self.status_code = status_code


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    """Fake credentials, so these tests see notify ENABLED without being able
    to reach the real chat. The suite-wide conftest fixture has already hidden
    the real ones and made requests.post raise; this only swaps in fakes on top
    of that, and the forbidden post stays in force unless a test asks for the
    `posts` fixture."""
    monkeypatch.setattr(notify, "config", FakeConfig())


@pytest.fixture
def posts(monkeypatch):
    """Capture outgoing requests instead of sending them."""
    calls = []

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return Response(200)

    monkeypatch.setattr(notify.requests, "post", post)
    return calls


@pytest.fixture
def sent(monkeypatch):
    """Capture message text at the send() seam."""
    texts = []
    monkeypatch.setattr(notify, "send", lambda text: texts.append(text) or True)
    return texts


# --- credentials: absent means silently off -------------------------------


def test_enabled_with_credentials():
    assert notify.enabled() is True


@pytest.mark.parametrize(
    "config",
    [
        FakeConfig(token=None, chat_id=None),  # config.py from HANDOFF.md
        FakeConfig(token=None),  # chat id only
        FakeConfig(chat_id=None),  # token only
        FakeConfig(token=""),  # placeholder left empty
        FakeConfig(chat_id=0),
        None,  # no config module at all
    ],
)
def test_disabled_without_complete_credentials(monkeypatch, config):
    monkeypatch.setattr(notify, "config", config)
    assert notify.enabled() is False


def test_send_is_a_no_op_when_disabled(monkeypatch):
    """The no_network fixture is the assertion: nothing may be posted. It must
    also stay silent - this is evaluated on every notified event, and a machine
    whose config.py predates Telegram must not accumulate noise."""
    monkeypatch.setattr(notify, "config", FakeConfig(token=None, chat_id=None))
    assert notify.send("anything") is False


def test_events_are_no_ops_when_disabled(monkeypatch, capsys):
    monkeypatch.setattr(notify, "config", FakeConfig(token=None, chat_id=None))
    assert notify.order_executed(order_record()) is False
    assert notify.startup_refused({}) is False
    assert notify.config_refused({}) is False
    assert notify.halt(**halt_values()) is False
    assert capsys.readouterr().out == ""


# --- transport -------------------------------------------------------------


def test_send_posts_plain_text_to_the_configured_chat(posts):
    assert notify.send("hello") is True
    (call,) = posts
    assert call["json"] == {"chat_id": str(CHAT_ID), "text": "hello"}
    assert TOKEN in call["url"]
    # No parse_mode: Telegram rejects a message whose markdown does not parse,
    # and a rejected notification is worse than an unstyled one.
    assert "parse_mode" not in call["json"]


def test_every_send_carries_a_short_timeout(posts):
    """At poll_seconds: 1 a hanging HTTP call blocks trading."""
    notify.send("hello")
    assert posts[0]["timeout"] == notify.TIMEOUT_SECONDS
    assert 0 < notify.TIMEOUT_SECONDS <= 5


def test_non_200_is_reported_as_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: Response(403))
    assert notify.send("hello") is False
    assert "403" in capsys.readouterr().out


# --- failure isolation -----------------------------------------------------


def raiser(exc):
    def post(*args, **kwargs):
        raise exc

    return post


@pytest.mark.parametrize(
    "exc",
    [
        OSError("connection reset"),
        # A localised OS error: an unguarded f-string of one of these on a
        # cp949 console is what killed the bot before.
        OSError(LOCALISED_ERROR),
        ValueError("nonsense"),
        KeyboardInterrupt,  # not an Exception: must still not escape? see below
    ],
)
def test_send_swallows_transport_failures(monkeypatch, exc, capsys):
    monkeypatch.setattr(notify.requests, "post", raiser(exc))
    if exc is KeyboardInterrupt:
        # Deliberately NOT swallowed: Ctrl-C is the operator stopping the bot,
        # and bot.run() handles it. Everything else is absorbed.
        with pytest.raises(KeyboardInterrupt):
            notify.send("hello")
        return
    assert notify.send("hello") is False
    out = capsys.readouterr().out
    assert out  # the failure is visible, not silent
    out.encode("ascii")  # and printable on a cp949 console


def test_failure_report_never_prints_the_token(monkeypatch, capsys):
    """requests puts the request URL - which carries the token - into the text
    of its own exceptions."""
    monkeypatch.setattr(
        notify.requests,
        "post",
        raiser(OSError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")),
    )
    assert notify.send("hello") is False
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert "<token>" in out


def test_a_formatter_that_raises_does_not_escape(monkeypatch, posts):
    monkeypatch.setattr(notify, "format_order", lambda record: 1 / 0)
    assert notify.order_executed(order_record()) is False
    assert posts == []


def test_a_send_that_raises_does_not_escape(monkeypatch):
    """send() absorbs its own failures; _notify assumes it does not."""
    monkeypatch.setattr(notify, "send", raiser(RuntimeError("boom")))
    assert notify.order_executed(order_record()) is False
    assert notify.halt(**halt_values()) is False
    assert notify.startup_refused({}) is False
    assert notify.config_refused({}) is False


# --- formatters: pure, ASCII, phone-readable ------------------------------


def order_record(**overrides):
    """The real recorded fill from futures/logs/orders-2026-09-16.jsonl."""
    path = Path(__file__).resolve().parent.parent / "futures/logs/orders-2026-09-16.jsonl"
    record = json.loads(path.read_text().splitlines()[-1])
    record.update(overrides)
    return record


def halt_values(**overrides):
    values = {
        "symbol": "ETHUSDT",
        "price": 1802.5,
        "trend": "long",
        "leverage": 5,
        "position_amt": 8.559,
        "position_notional": 15427.59,
        "outcome": notify.NOT_FLATTENED,
    }
    values.update(overrides)
    return values


def refusal_record(**overrides):
    record = {
        "action": "startup_refused",
        "reason": "unreconciled_position",
        "symbol": "ETHUSDT",
        "testnet": True,
        "position_amt": 8.559,
        "current_notional": 20497.12,
        "max_notional": 25000.0,
        "trend": "long",
        "wrong_side": False,
    }
    record.update(overrides)
    return record


def all_messages():
    return [
        notify.format_order(order_record()),
        notify.format_order(order_record(fill_price=2395.06, notional=2098.07, reduce_only=True)),
        notify.format_halt(**halt_values()),
        notify.format_halt(
            **halt_values(
                outcome=notify.FLATTENED,
                record=order_record(
                    side="SELL", quantity=8.559, fill_price=1802.4, notional=15426.73,
                    position_before=8.559, position_after=0.0,
                ),
            )
        ),
        notify.format_halt(
            **halt_values(
                outcome=notify.FLATTEN_FAILED,
                side="SELL",
                quantity=8.559,
                error=RuntimeError("-2019 Margin is insufficient"),
            )
        ),
        notify.format_loop_error("-2019 Margin is insufficient", symbol="ETHUSDT"),
        notify.format_startup_refused(refusal_record()),
        notify.format_config_refused(
            {"reason": "testnet_change_requires_restart", "current_testnet": True,
             "rejected_testnet": False}
        ),
        notify.format_config_refused(
            {"reason": "symbol_change_with_open_position", "current_symbol": "ETHUSDT",
             "rejected_symbol": "BTCUSDT", "position_amt": 8.559}
        ),
    ]


def test_every_message_is_ascii_and_phone_shaped():
    for text in all_messages():
        text.encode("ascii")  # Telegram is fine either way; the console is not
        lines = text.split("\n")
        assert len(max(lines, key=len)) <= 70  # readable without wrapping
        assert lines[0].isupper() or lines[0].startswith("HALT")  # leads with the event
        for marker in ("*", "`"):  # no hand-rolled markdown
            assert marker not in text
    # Underscores DO appear, in the strategy's own reason names (scale_in,
    # halt_flatten). They are literal because no parse_mode is sent - see
    # test_send_posts_plain_text_to_the_configured_chat.


def test_order_message_carries_the_decision_and_the_settings():
    text = notify.format_order(order_record())
    assert text.startswith("ORDER FILLED\nBUY 0.876 ETHUSDT")
    for fragment in [
        "reason: scale_in",  # the decision
        "zone 1: 2,371.26 - 2,625.00",
        "d: 0.906",
        "target notional: 20,499.73 USDT",
        "position: 7.683 -> 8.559",
        "ETHUSDT long 5x",  # the settings that produced it
        "alpha 2.0, exposure 1.0",
        "order id: 16795863483",
    ]:
        assert fragment in text


def test_order_message_says_so_when_the_exchange_reported_no_fill():
    """avgPrice "0.00" with no cumQuote. Substituting the pre-trade mark price
    is what made the old records understate trading cost."""
    text = notify.format_order(order_record())
    assert "fill: not reported by the exchange" in text
    assert "fill price:" not in text


def test_order_message_shows_the_fill_when_there_is_one():
    text = notify.format_order(order_record(fill_price=2395.06, notional=2098.07))
    assert "fill price: 2,395.06" in text
    assert "fill notional: 2,098.07 USDT" in text


def test_order_message_marks_a_reduce_only_order():
    assert "reduce only: yes" in notify.format_order(order_record(reduce_only=True))
    assert "reduce only" not in notify.format_order(order_record(reduce_only=False))


def test_order_message_survives_a_record_full_of_holes():
    """A renamed or missing field must cost detail, never an exception on the
    path that just moved real money."""
    text = notify.format_order({})
    assert text.startswith("ORDER FILLED")
    assert "unknown" in text
    notify.format_order({"quantity": "n/a", "position_after": None, "mark_price": "x"})


def test_halt_message_says_no_order_went_out_and_why():
    assert "no position to flatten" in notify.format_halt(
        **halt_values(position_amt=0.0, position_notional=0.0)
    )
    # Residual dust below the exchange minimum: decide() says SCALE_OUT/HALT
    # forever and is_executable rejects it every time, so nothing is sent.
    assert "too small to close" in notify.format_halt(
        **halt_values(position_amt=0.0001, position_notional=0.18)
    )


def test_halt_message_reports_the_completed_flatten():
    """The message waits for the flatten, so it can say what became of the
    position instead of only that the bot decided to get out."""
    text = notify.format_halt(
        **halt_values(
            outcome=notify.FLATTENED,
            record=order_record(
                side="SELL", quantity=8.559, fill_price=1802.4, notional=15426.73,
                position_before=8.559, position_after=0.0,
            ),
        )
    )
    assert text.startswith("HALT - FLATTENED")
    assert "SELL 8.559 at 1,802.40" in text
    assert "fill notional: 15,426.73 USDT" in text
    assert "position: 8.559 -> 0" in text


def test_halt_message_shouts_when_the_flatten_failed():
    """The single most urgent message the bot can send: thesis dead, exit
    attempted, exit refused, position still on."""
    text = notify.format_halt(
        **halt_values(
            outcome=notify.FLATTEN_FAILED,
            side="SELL",
            quantity=8.559,
            error=RuntimeError("-2019 Margin is insufficient"),
        )
    )
    assert text.startswith("HALT - FLATTEN FAILED")
    assert "REJECTED" in text
    assert "still open: 8.559 (15,427.59 USDT)" in text
    assert "tried: SELL 8.559" in text
    assert "error: -2019 Margin is insufficient" in text
    assert "Check the position NOW." in text


def test_halt_failure_message_redacts_and_clips_the_error():
    text = notify.format_halt(
        **halt_values(
            outcome=notify.FLATTEN_FAILED,
            error=OSError(f"url: /bot{TOKEN}/sendMessage " + "x" * 500),
        )
    )
    assert TOKEN not in text
    assert "<token>" in text
    assert len(text) < 600  # a stray HTML error page cannot become the message


def test_loop_error_message_names_the_symbol_and_says_the_bot_is_alive():
    text = notify.format_loop_error("-2019 Margin is insufficient", symbol="ETHUSDT")
    assert text.startswith("BOT ERROR\nETHUSDT")
    assert "-2019 Margin is insufficient" in text
    assert "still running" in text
    assert "once a minute" in text  # so a repeat is not read as a new failure


def test_loop_error_message_forces_ascii():
    """The exception text comes from the OS or the exchange, and on this
    machine it can arrive localised."""
    text = notify.format_loop_error(LOCALISED_ERROR)
    text.encode("ascii")


def test_startup_refused_message_distinguishes_the_two_reasons():
    assert "exceeds the exposure cap" in notify.format_startup_refused(refusal_record())
    assert "opposes the trend" in notify.format_startup_refused(
        refusal_record(wrong_side=True)
    )
    assert "is NOT running" in notify.format_startup_refused(refusal_record())
    assert "LIVE" in notify.format_startup_refused(refusal_record(testnet=False))


def test_config_refused_message_says_nothing_was_applied():
    for text in all_messages()[-2:]:
        assert "REFUSED" in text
        assert "NO part of the new settings file was applied." in text


def test_config_refused_names_the_rejected_edit():
    testnet, symbol = all_messages()[-2:]
    assert "'testnet' True -> False needs a RESTART." in testnet
    assert "Still running on TESTNET" in testnet
    assert "'symbol' ETHUSDT -> BTCUSDT while ETHUSDT holds 8.559." in symbol


# --- wiring: what the running loop actually notifies -----------------------
#
# These drive the real bot.run() through the same harness the tick-throttle
# tests use, because a notifier that is correct on its own but wired to the
# wrong place would pass every test above.


@pytest.fixture
def written(monkeypatch):
    records = []
    monkeypatch.setattr(journal, "log_tick", lambda record, log_dir=None: records.append(record))
    return records


@pytest.fixture
def events(monkeypatch):
    """One ordered log of orders placed and messages sent, so a test can assert
    that the money path ran FIRST."""
    log = []
    monkeypatch.setattr(notify, "send", lambda text: log.append(("notify", text)) or True)
    return log


class Holding(loop.LoopClient):
    """A client that already holds a position, for the flatten paths.

    1.0 ETH at 1500 is 1500 USDT against a 5000 cap, so the startup
    reconciliation guard lets the bot start; 1500 is below the ladder, so the
    first tick halts."""

    def __init__(self, events=None, amt="1.0", order_error=None):
        super().__init__(price="1500.00", balance="1000.0", order_error=order_error)
        self.amt = amt
        self.events = [] if events is None else events

    def get_position_risk(self, symbol=None):
        return [{"symbol": "ETHUSDT", "positionAmt": self.amt, "unRealizedProfit": "0.0"}]

    def new_order(self, **params):
        # Logged BEFORE the (possibly raising) call: what is being asserted is
        # that the attempt happened, and that it happened first.
        self.events.append(("order", params.get("side")))
        result = super().new_order(**params)
        self.amt = "0.0"
        return {**result, "avgPrice": "1500.00", "executedQty": "1.0", "cumQuote": "1500.00"}


class Trimming(loop.LoopClient):
    """A client parked on the dead-band exit, for the repeated-FILL paths.

    In-zone SCALE_IN/SCALE_OUT rests on the book as a limit-order ladder now, so
    a client sitting inside a zone no longer produces the market-order fills
    these tests are about. 3400 is above every resistance - off the ladder, but
    nowhere near the adverse end that would HALT - so each tick decides the
    dead-band SCALE_OUT, which still goes out as a MARKET order. The position is
    deliberately left unchanged by the fill so the state repeats every tick,
    which is what makes 'never throttled' meaningful: the tick record is a
    verbatim repeat and only `forced` keeps it, one per order."""

    def __init__(self, amt="1.0"):
        super().__init__(price="3400.00", balance="1000.0")
        self.amt = amt

    def get_position_risk(self, symbol=None):
        return [{"symbol": "ETHUSDT", "positionAmt": self.amt, "unRealizedProfit": "0.0"}]

    def new_order(self, **params):
        result = super().new_order(**params)
        return {**result, "avgPrice": "3400.00", "executedQty": "1.0", "cumQuote": "3400.00"}


def messages(events):
    return [text for kind, text in events if kind == "notify"]


def test_a_flatten_is_placed_before_anything_is_notified(monkeypatch, written, events):
    """The ordering the money path depends on: HALT is the response to an
    adverse move, so the exit order goes out first and the message reports it
    afterwards. A hanging Telegram cannot cost a tick of slippage."""
    api = Holding(events=events)
    drive(monkeypatch, api, ticks=2)

    assert [kind for kind, _ in events][0] == "order"
    sent = messages(events)
    assert sent[0].startswith("ORDER FILLED")  # the receipt, never throttled
    assert sent[1].startswith("HALT - FLATTENED")  # the alarm, with the outcome
    assert "SELL 1 at 1,500.00" in sent[1]
    assert "position: 1 -> 0" in sent[1]
    assert len(sent) == 2  # the halt does not repeat on the next tick


def test_a_failed_flatten_is_attempted_first_and_then_shouted_about(monkeypatch, written, events):
    """execute() raising must not skip the notification: by the next tick
    `halted` has latched, so nothing would ever say a HALT had happened."""
    api = Holding(events=events, order_error=lambda n: "-2019 Margin is insufficient")
    drive(monkeypatch, api, ticks=2)

    assert [kind for kind, _ in events][0] == "order"
    assert api.orders == 2  # it kept trying after the failure
    sent = messages(events)
    assert sent[0].startswith("HALT - FLATTEN FAILED")
    assert "still open: 1 (1,500.00 USDT)" in sent[0]
    assert "tried: SELL 1" in sent[0]
    assert "-2019 Margin is insufficient" in sent[0]
    # The generic loop-error push follows from the same exception, then the
    # identical repeat on the next tick is throttled away.
    assert [text.split("\n")[0] for text in sent] == ["HALT - FLATTEN FAILED", "BOT ERROR"]


def test_a_dead_notifier_does_not_hold_up_the_flatten(monkeypatch, written):
    monkeypatch.setattr(notify, "send", raiser(OSError("telegram is down")))
    orders = []
    monkeypatch.setattr(journal, "log_order", lambda record, log_dir=None: orders.append(record))

    api = Holding()
    drive(monkeypatch, api, ticks=2)

    assert api.orders == 1  # placed, and the position is flat afterwards
    assert orders[0]["reason"] == "halt_flatten"
    assert orders[0]["position_after"] == 0.0


def test_a_repeating_failure_notifies_once_per_interval_not_once_per_poll(
    monkeypatch, written, sent
):
    """-2019 repeats on every poll once required margin exceeds the wallet -
    which is where exposure_fraction 1.0 puts the strategy at its largest. The
    bot is wedged retrying while holding a leveraged position, and looks
    healthy from outside; one message a minute says so without 300 of them."""
    api = loop.LoopClient(balance="1000.0", order_error=lambda n: "-2019 Margin is insufficient")
    loop.run_loop(monkeypatch, written, ticks=300, api=api)

    records = [r for r in written if r.get("action") == "error"]
    pushed = [text for text in sent if text.startswith("BOT ERROR")]
    assert api.orders == 300  # the bot kept trying, as it should
    assert len(records) == len(pushed) == 5  # exactly one message per record


def test_a_distinct_failure_notifies_the_instant_it_happens(monkeypatch, written, sent):
    api = loop.LoopClient(
        balance="1000.0",
        order_error=lambda n: "-2019 Margin is insufficient" if n <= 90 else "-1021 Timestamp",
    )
    loop.run_loop(monkeypatch, written, ticks=95, api=api)

    pushed = [text for text in sent if text.startswith("BOT ERROR")]
    assert len(pushed) == 3  # first failure, 60s later, then the new one
    assert "-1021 Timestamp" in pushed[-1]  # the tick it changed, not 60s after


def test_every_fill_notifies_exactly_once_never_throttled(monkeypatch, written, sent):
    """Sticky states throttle; fills do not. Each one moves real money."""
    api = Trimming()
    loop.run_loop(monkeypatch, written, ticks=4, api=api)

    assert api.orders == len(sent) > 1
    assert all(text.startswith("ORDER FILLED") for text in sent)


def test_halt_notifies_on_the_transition_not_every_tick(monkeypatch, written, sent):
    """Once price leaves the ladder EVERY tick decides HALT. One message."""
    api = loop.LoopClient(price="1500.00", balance="1000.0")
    loop.run_loop(monkeypatch, written, ticks=300, api=api)

    assert len(sent) == 1
    assert sent[0].startswith("HALT - price left the zone ladder")
    assert "ETHUSDT at 1,500.00" in sent[0]


def test_a_second_departure_notifies_again(monkeypatch, written, sent):
    """The halt announcement re-arms when price returns to the ladder, so the
    next departure - with whatever position was taken in between - is not
    silently swallowed by the first one."""

    class Wandering(loop.LoopClient):
        """Off the ladder, back onto it, then off again."""

        def __init__(self):
            super().__init__(balance="1000.0")
            self.prices = ["1500.00"] * 3 + ["2700.00"] * 3 + ["1500.00"] * 3
            self.reads = 0

        def ticker_price(self, symbol):
            price = self.prices[min(self.reads, len(self.prices) - 1)]
            self.reads += 1
            return {"symbol": symbol, "price": price}

    api = Wandering()
    loop.run_loop(monkeypatch, written, ticks=9, api=api)

    headlines = [text.split("\n")[0] for text in sent]
    assert headlines.count("HALT - price left the zone ladder") == 2
    # It was back on the ladder in between and acted there, which is what makes
    # the second departure a new one rather than the first still latched. That
    # activity is a limit-order ladder now, not a market fill, so there is no
    # ORDER FILLED message to look for - the placement itself is the evidence.
    assert api.orders > 0
    assert all(o["type"] == "LIMIT" for o in api.placed_orders)


def drive(monkeypatch, api, ticks):
    """Run bot.run() for `ticks` polls against `api`, on the real TickLog.

    loop.run_loop stubs out journal.log_order, which is the very thing two of
    these tests are about, so they drive the loop directly instead."""
    monkeypatch.setattr(bot.client, "build", lambda testnet: api)
    polls = {"n": 0}

    def sleep(seconds):
        polls["n"] += 1
        if polls["n"] >= ticks:
            raise KeyboardInterrupt

    monkeypatch.setattr(bot, "_sleep", sleep)
    try:
        bot.run()
    except KeyboardInterrupt:
        pass


def test_a_broken_notifier_costs_nothing_but_the_notification(monkeypatch, written):
    """The proof that this feature cannot break trading: with send() raising on
    every call, the orders still go out and the journal still records them."""
    monkeypatch.setattr(notify, "send", raiser(OSError("telegram is down")))

    orders = []
    monkeypatch.setattr(journal, "log_order", lambda record, log_dir=None: orders.append(record))

    api = Trimming()
    drive(monkeypatch, api, ticks=4)

    assert api.orders == len(orders) == 4  # every order placed, every one journalled
    assert [r.get("action") for r in written].count("error") == 0  # never reached the handler


def test_startup_refusal_notifies_and_the_bot_still_refuses(monkeypatch, written, sent):
    api = loop.LoopClient(balance="1000.0")
    monkeypatch.setattr(api, "get_position_risk", lambda symbol=None: [
        {"symbol": "ETHUSDT", "positionAmt": "-5.0", "unRealizedProfit": "0.0"}
    ])
    drive(monkeypatch, api, ticks=1)  # run() returns before it ever sleeps

    assert api.orders == 0
    assert [r["action"] for r in written] == ["startup_refused"]
    assert len(sent) == 1
    assert sent[0].startswith("STARTUP REFUSED")
    assert "opposes the trend" in sent[0]


def test_format_liquidation_brake_reports_a_shrink():
    text = notify.format_liquidation_brake(
        symbol="ETHUSDT", trend="long", leverage=5,
        survival_price=1780.0, liquidation_price=1850.0,
        scale=0.4, cancelled=False,
    )
    assert "LIQUIDATION BRAKE" in text
    assert "1,780.00" in text
    assert "1,850.00" in text
    assert "40" in text  # scale shown as a percentage


def test_format_liquidation_brake_reports_a_full_cancel():
    text = notify.format_liquidation_brake(
        symbol="ETHUSDT", trend="long", leverage=5,
        survival_price=1780.0, liquidation_price=1900.0,
        scale=0.0, cancelled=True,
    )
    assert "CANCELLED" in text.upper()


def test_liquidation_brake_event_sends(sent):
    notify.liquidation_brake(
        symbol="ETHUSDT", trend="long", leverage=5,
        survival_price=1780.0, liquidation_price=1850.0,
        scale=0.4, cancelled=False,
    )
    assert len(sent) == 1
    assert "LIQUIDATION BRAKE" in sent[0]
