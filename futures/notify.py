"""Telegram notifications for the trading loop.

Formatting and sending, and nothing else. No trading logic lives here, and no
decision is ever taken here: bot.py decides that something happened and hands
over the values it already assembled for the journal.

Three properties matter more than anything this module actually says:

1. It cannot raise into the loop. The bot has already been killed once by an
   exception thrown inside an exception handler while it held a leveraged
   position. Every public entry point swallows Exception - including the
   failure path itself, which is why the report helper redacts and forces
   ASCII inside its own try: this machine's console is cp949, and an f-string
   of a localised OSError is exactly how that crash happened.
2. It cannot stall the loop for long. At poll_seconds: 1 a hanging HTTP call
   blocks trading, so every request carries an explicit short timeout. There is
   deliberately no thread and no queue: a bounded worst-case stall on a rare
   event beats a background worker nobody supervises.
3. It disappears when unconfigured. A config.py without telegram_token /
   telegram_chat_id (HANDOFF.md documents recreating that file from scratch)
   means notifications are off - not an error, not a warning every tick.

The token is a credential, and requests puts the request URL into the text of
its own exceptions, so nothing derived from an exception is printed before
_redact has been over it.
"""

import requests

try:
    # Optional on purpose: notifications are a convenience, so a config.py that
    # does not exist or does not import must leave the bot able to trade.
    import config
except Exception:  # pragma: no cover - config.py is present wherever the bot runs
    config = None

# Long enough for a normal round-trip to Telegram, short enough that the
# worst case is a stall the operator would not notice. Orders are rare (the
# churn guard sees to that), so this is paid at most a few times a day.
TIMEOUT_SECONDS = 3.0

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

_UNKNOWN = "unknown"

# What became of the position on the tick that halted. See format_halt.
FLATTENED = "flattened"
FLATTEN_FAILED = "flatten_failed"
NOT_FLATTENED = "not_flattened"


# --- credentials and transport --------------------------------------------


def _credentials() -> tuple[str, str] | None:
    """(token, chat_id), or None when notifications are not configured.

    Read on every call rather than captured at import: a test can swap the
    config module, and an operator who adds credentials does not have to be
    told the bot must be restarted to pick them up."""
    token = getattr(config, "telegram_token", None)
    chat_id = getattr(config, "telegram_chat_id", None)
    if not token or not chat_id:
        return None
    return str(token), str(chat_id)


def enabled() -> bool:
    """Whether credentials are present. Absent means silently disabled."""
    return _credentials() is not None


def _redact(text: str) -> str:
    """Remove the bot token from text that is about to be printed.

    requests embeds the full request URL in its exception messages, and that
    URL carries the token. Without this, one connection error would print the
    credential to the console and into whatever captures it."""
    token = getattr(config, "telegram_token", None)
    if token:
        text = text.replace(str(token), "<token>")
    return text


def _report(what: str, detail) -> None:
    """Print one line about a failed notification. Cannot itself raise.

    Only ever called on a real send attempt, so a disabled or idle bot prints
    nothing. str(detail), the redaction and the encode are all inside the
    guard: a localised OSError that the cp949 console cannot encode must not
    become the thing that ends an unattended bot."""
    try:
        message = _redact(str(detail)).encode("ascii", "replace").decode("ascii")
        print(f"notify: {what}: {message}")
    except Exception:
        pass


def send(text: str) -> bool:
    """Post one message to Telegram. Never raises. Returns whether it landed.

    Plain text, no parse_mode: Telegram rejects a message whose markdown does
    not parse, and a rejected notification about a filled order is worse than
    an unstyled one."""
    credentials = _credentials()
    if credentials is None:
        return False
    token, chat_id = credentials

    try:
        response = requests.post(
            _API_URL.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            return True
        _report("telegram rejected the message", f"HTTP {response.status_code}")
        return False
    except Exception as exc:
        _report("send failed", exc)
        return False


def _notify(formatter, *args, **kwargs) -> bool:
    """Format and send, absorbing anything either of them does.

    Both halves are inside the guard. send() already swallows its own
    failures and the formatters are written not to raise, but this is the only
    frame between them and a loop holding a leveraged position, so it assumes
    neither: a renamed record field must degrade to a worse message, never to
    an exception on the path that just moved real money."""
    if not enabled():
        return False
    try:
        return send(formatter(*args, **kwargs))
    except Exception as exc:
        _report("notification dropped", exc)
        return False


# --- value formatting ------------------------------------------------------
#
# Every helper below turns an unusable value into the word "unknown" instead of
# raising, because a notification that says it does not know one number is
# still worth sending.


def _num(value, digits: int = 2) -> str:
    """Fixed-precision number with thousands separators, or "unknown"."""
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return _UNKNOWN


def _trim(value, digits: int = 8) -> str:
    """Quantity with trailing zeros stripped (0.87600000 -> 0.876)."""
    try:
        text = f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return _UNKNOWN
    text = text.rstrip("0").rstrip(".")
    return text or "0"


def _product(a, b):
    """a * b, or None if either is missing."""
    try:
        return float(a) * float(b)
    except (TypeError, ValueError):
        return None


def _known(text: str) -> bool:
    return text != _UNKNOWN


def _safe_text(value, limit: int = 200) -> str:
    """Text the bot did not write - an exchange or OS error message - made fit
    to put in a notification.

    Redacted (an exception raised anywhere near this module can carry the
    request URL, and the token with it), forced to ASCII so that every message
    this module produces is ASCII whatever the OS locale hands over, and
    clipped so a stray HTML error page cannot become the notification."""
    text = _redact(str(value)).encode("ascii", "replace").decode("ascii")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


# --- formatters ------------------------------------------------------------
#
# Pure: plain values in, one string out. No network, no clock, no config. That
# is what makes the wording testable without a Telegram account.
#
# House style, because these are read on a phone: ASCII only, no markdown, one
# fact per line, and the first line says what happened.


def format_order(record: dict) -> str:
    """Message for an executed order, from the order-journal record.

    Takes the same dict that goes to journal.log_order so the message and the
    audit trail cannot disagree: nothing here is recomputed from the decision,
    it is only rendered. The one derived value is the resulting position's
    notional, which is position_after priced at the record's own mark."""
    symbol = record.get("symbol", _UNKNOWN)
    price = _num(record.get("fill_price"))
    notional = _num(record.get("notional"))
    actual = _product(record.get("position_after"), record.get("mark_price"))

    lines = [
        "ORDER FILLED",
        f"{record.get('side', _UNKNOWN)} {_trim(record.get('quantity'))} {symbol}",
    ]

    if _known(price) or _known(notional):
        lines.append(f"fill price: {price}")
        lines.append(f"fill notional: {notional} USDT")
    else:
        # avgPrice "0.00" with no cumQuote: the exchange has not reported the
        # fill yet. Say so rather than dress the pre-trade mark up as a fill.
        lines.append("fill: not reported by the exchange")

    zone_index = record.get("active_zone_index")
    lines += [
        "",
        f"reason: {record.get('reason', _UNKNOWN)}",
        f"zone {zone_index if zone_index is not None else _UNKNOWN}: "
        f"{_num(record.get('support'))} - {_num(record.get('resistance'))}",
        f"d: {_num(record.get('d'), 3)}",
        f"mark: {_num(record.get('mark_price'))}",
        f"target notional: {_num(record.get('target_notional'))} USDT",
        f"actual notional: {_num(actual)} USDT",
        f"position: {_trim(record.get('position_before'))} -> "
        f"{_trim(record.get('position_after'))}",
    ]

    if record.get("reduce_only"):
        lines.append("reduce only: yes")

    lines += [
        "",
        f"{symbol} {record.get('trend', _UNKNOWN)} {record.get('leverage', _UNKNOWN)}x",
        f"alpha {record.get('alpha', _UNKNOWN)}, "
        f"exposure {record.get('exposure_fraction', _UNKNOWN)}",
        f"balance: {_num(record.get('balance'))} USDT",
        f"order id: {record.get('order_id', _UNKNOWN)}",
    ]
    return "\n".join(lines)


def format_halt(
    symbol: str,
    price: float,
    trend: str,
    leverage: int,
    position_amt: float,
    position_notional: float,
    outcome: str = NOT_FLATTENED,
    record: dict | None = None,
    side: str | None = None,
    quantity: float | None = None,
    error=None,
) -> str:
    """Message for the tick that halts: price has left the zone ladder.

    The one an unattended operator would most regret missing - after this the
    bot stops scaling, and nothing else raises an alarm.

    Sent AFTER the flatten has been attempted, never before it. HALT means
    price left the ladder in the direction that kills the thesis, and the
    flatten IS the response to that; a hanging Telegram must not hold a market
    order on a leveraged position for even one tick. Waiting also makes the
    message strictly better, because it can say what became of the position
    rather than only that the bot decided to get out.

    `outcome` selects which of the three things happened, and only the
    arguments for that branch need to be supplied:
      FLATTENED      - the exit filled; pass the order-journal `record`.
      FLATTEN_FAILED - execute() raised; pass `side`, `quantity`, `error`.
      NOT_FLATTENED  - no order went out at all."""
    context = [f"{symbol} at {_num(price)}", f"{trend} {leverage}x"]

    if outcome == FLATTEN_FAILED:
        # The most urgent message this bot can send: it concluded the thesis
        # was dead, tried to get out, and could not. The position is still on.
        return "\n".join(
            [
                "HALT - FLATTEN FAILED",
                "The exit order was REJECTED.",
                *context,
                f"still open: {_trim(position_amt)} ({_num(position_notional)} USDT)",
                f"tried: {side} {_trim(quantity)}",
                f"error: {_safe_text(error)}",
                "",
                "The bot retries every poll. Check the position NOW.",
            ]
        )

    if outcome == FLATTENED:
        filled = record or {}
        return "\n".join(
            [
                "HALT - FLATTENED",
                "Price left the zone ladder; the position is closed.",
                *context,
                f"{filled.get('side', _UNKNOWN)} {_trim(filled.get('quantity'))} at "
                f"{_num(filled.get('fill_price'))}",
                f"fill notional: {_num(filled.get('notional'))} USDT",
                f"position: {_trim(filled.get('position_before'))} -> "
                f"{_trim(filled.get('position_after'))}",
                "",
                "No further scaling while price is off the ladder.",
            ]
        )

    lines = [
        "HALT - price left the zone ladder",
        *context,
        f"position: {_trim(position_amt)} ({_num(position_notional)} USDT)",
    ]
    if position_amt:
        # Sized below the exchange minimum, so it cannot be closed by this bot.
        lines.append("position left open: too small to close")
    else:
        lines.append("no position to flatten")
    lines.append("No further scaling while price is off the ladder.")
    return "\n".join(lines)


def format_loop_error(message, symbol=None) -> str:
    """Message for a fault the loop absorbed and journalled.

    Worth pushing because the loop survives these by design, so a bot wedged
    on one looks perfectly healthy from outside: with exposure_fraction at 1.0
    the required margin exceeds the wallet as d approaches 1, Binance rejects
    with -2019, and the bot retries every second - holding a leveraged
    position - for as long as that lasts."""
    lines = ["BOT ERROR"]
    if symbol:
        lines.append(str(symbol))
    lines += [
        _safe_text(message),
        "",
        "The loop is still running. An identical repeat is",
        "notified at most once a minute.",
    ]
    return "\n".join(lines)


def format_startup_refused(record: dict) -> str:
    """Message for a startup the bot declined, from its journal record."""
    problem = "opposes the trend" if record.get("wrong_side") else "exceeds the exposure cap"
    amt = record.get("position_amt")
    return "\n".join(
        [
            "STARTUP REFUSED",
            f"{record.get('symbol', _UNKNOWN)} already holds a position that {problem}.",
            f"position: {_trim(amt)} ({_num(record.get('current_notional'))} USDT)",
            f"trend: {record.get('trend', _UNKNOWN)}",
            f"this bot's cap: {_num(record.get('max_notional'))} USDT",
            f"mode: {'TESTNET' if record.get('testnet') else 'LIVE'}",
            "",
            "The bot is NOT running. Flatten or reconcile that",
            "position manually, then start it again.",
        ]
    )


def format_config_refused(record: dict) -> str:
    """Message for a settings edit that did NOT take effect.

    Worth a push notification precisely because the operator has every reason
    to believe it did - they edited the file and the bot kept running."""
    reason = record.get("reason")
    lines = ["SETTINGS RELOAD REFUSED"]

    if reason == "testnet_change_requires_restart":
        current = record.get("current_testnet")
        lines += [
            f"'testnet' {current} -> {record.get('rejected_testnet')} needs a RESTART.",
            f"Still running on {'TESTNET' if current else 'LIVE'} with the old settings.",
        ]
    elif reason == "symbol_change_with_open_position":
        current = record.get("current_symbol", _UNKNOWN)
        lines += [
            f"'symbol' {current} -> {record.get('rejected_symbol')} "
            f"while {current} holds {_trim(record.get('position_amt'))}.",
            f"Still running on {current}.",
        ]
    else:  # pragma: no cover - a reason added to bot.py without one added here
        lines.append(f"reason: {reason}")

    lines += ["", "NO part of the new settings file was applied."]
    return "\n".join(lines)


# --- events ----------------------------------------------------------------
#
# What bot.py calls. One line per event at the call site, and each of them
# returns a bool rather than raising, whatever happens underneath.


def order_executed(record: dict) -> bool:
    """Never throttled: fills are rare and each one moves real money."""
    return _notify(format_order, record)


def halt(**values) -> bool:
    """Call on the TRANSITION into HALT only - every tick decides HALT for as
    long as price stays off the ladder - and only once the flatten has been
    attempted, so the message can report what became of the position."""
    return _notify(format_halt, **values)


def loop_error(message, symbol=None) -> bool:
    """Call only where the loop's error record was actually WRITTEN.

    That reuses the error journal's own throttle state instead of adding a
    second one: a new, distinct failure notifies the instant it happens, and
    an identical repeat - a rejected order recurring every poll - collapses to
    one message a minute rather than one per poll."""
    return _notify(format_loop_error, message, symbol=symbol)


def startup_refused(record: dict) -> bool:
    return _notify(format_startup_refused, record)


def config_refused(record: dict) -> bool:
    """Call where bot.py announces the refusal, i.e. only when the refused
    config changed - the file is re-read and re-refused every tick."""
    return _notify(format_config_refused, record)
