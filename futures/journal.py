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
