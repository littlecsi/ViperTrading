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
    """One line per record the caller hands over: the loop's audit trail and
    the state/action history a PPO policy trains against.

    This function writes whatever it is given; the cadence is the caller's
    decision, and bot.py does not call it on every iteration. Uneventful ticks
    (HOLD/IDLE) are throttled there to one per bot.TICK_LOG_INTERVAL_SECONDS,
    while every tick where something happened, and every non-decision record,
    is always passed here. So a gap between consecutive uneventful ticks is
    expected and is not a dropped write. Note what is NOT done: ticks are never
    sampled one-in-N, because that would thin out exactly the events this log
    exists to capture."""
    _append("ticks", record, log_dir)


def log_order(record: dict, log_dir: str | None = None) -> None:
    """One line per executed order, carrying the full environment snapshot at
    fill time so a trade's context is never reconstructed by joining logs."""
    _append("orders", record, log_dir)
