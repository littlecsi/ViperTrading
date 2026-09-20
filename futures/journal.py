import json
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = str(Path(__file__).resolve().parent / "logs")


def _append(prefix: str, record: dict, log_dir: str | None) -> None:
    directory = Path(log_dir or LOG_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    record = {"timestamp": now.isoformat(), **record}

    path = directory / f"{prefix}-{now.date().isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def log_tick(record: dict, log_dir: str | None = None) -> None:
    """Append one line per record handed over: the loop's audit trail and the
    state/action history a PPO policy trains against.

    Everything given to it is written. What is NOT done is sample one record in
    N - that would thin out exactly the events this log exists to capture.

    The cadence is not this module's to describe: bot.py does not call this
    every iteration, and bot.TickLog owns the rule for which records are
    written and when. A gap between records is therefore expected rather than a
    dropped write. Read TickLog for the policy; restating it here only earns a
    docstring that goes stale the next time the policy changes."""
    _append("ticks", record, log_dir)


def log_order(record: dict, log_dir: str | None = None) -> None:
    """One line per executed order, carrying the full environment snapshot at
    fill time so a trade's context is never reconstructed by joining logs."""
    _append("orders", record, log_dir)


# --- reading back ----------------------------------------------------------
#
# This module wrote and never read until startup fill recovery needed to ask
# "what do I already know about?". Answering that from the journal itself is
# deliberate: the alternative is a second durable file holding the last
# recovered trade id, which can disagree with the journal, be lost, or be
# restored from a stale backup - and every one of those failures ends in either
# a duplicated row or a silently skipped fill. The journal is the audit trail,
# so it is also the memory.
#
# Everything below tolerates damage rather than raising. Recovery runs at
# startup, on journals last written by a process that was killed - a truncated
# final line is the normal shape of that, not an exception.


def _read_records(prefix: str, log_dir: str | None):
    """Yield every parseable record from one stream's daily files.

    One file per day means the newest record is not in whichever filename
    sorts last - a day with no activity leaves no file at all - so callers
    that want a maximum must look across all of them.

    A line that will not parse is skipped, not raised on. Losing one damaged
    record is the cost of reading the rest; aborting would leave recovery with
    no anchor at all and hand back exactly the blind spot it exists to close."""
    directory = Path(log_dir or LOG_DIR)
    if not directory.is_dir():
        return

    for path in sorted(directory.glob(f"{prefix}-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _record_ms(record: dict) -> int | None:
    """When a record's event happened, in epoch milliseconds.

    `fill_time` is the exchange's own matching time and is exact, but only
    recovered rows carry it - the live path deliberately spends no userTrades
    call per fill. Everything else falls back to the journal's own timestamp,
    which is when the record was WRITTEN and therefore runs slightly late.
    recovery.SAFETY_MARGIN_MS exists to absorb that difference."""
    fill_time = record.get("fill_time")
    if isinstance(fill_time, (int, float)):
        return int(fill_time)

    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return int(datetime.fromisoformat(stamp).timestamp() * 1000)
    except ValueError:
        return None


def _last_time(prefix: str, log_dir: str | None) -> int | None:
    times = [ms for ms in (_record_ms(r) for r in _read_records(prefix, log_dir))
             if ms is not None]
    return max(times) if times else None


def last_order_time(log_dir: str | None = None) -> int | None:
    """Epoch ms of the newest order record, or None if there are none.

    None means "no evidence", and recovery must not read it as 0 - an anchor
    at the epoch would sweep in every trade the account has ever made."""
    return _last_time("orders", log_dir)


def last_tick_time(log_dir: str | None = None) -> int | None:
    """Epoch ms of the newest tick record, or None.

    The cold-start anchor. The order journal is empty on a fresh install and
    on any run that never filled, but the tick journal still marks when this
    bot was last provably alive - which bounds the window in which it could
    have had orders working."""
    return _last_time("ticks", log_dir)


def known_order_ids(log_dir: str | None = None, since_ms: int = 0) -> set:
    """Order ids already in the order journal at or after `since_ms`.

    What makes running recovery on every single startup safe: anything in here
    is skipped, so a restart cannot write a fill a previous one already wrote.
    Bounded by `since_ms` so the set stays proportional to the recovery window
    rather than to the journal's whole history."""
    ids = set()
    for record in _read_records("orders", log_dir):
        order_id = record.get("order_id")
        if order_id is None:
            continue
        at = _record_ms(record)
        if at is not None and at < since_ms:
            continue
        try:
            ids.add(int(order_id))
        except (TypeError, ValueError):
            continue
    return ids
