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
    """One line per loop iteration, including no-ops. This is the continuous
    state/action history a PPO policy trains against."""
    _append("ticks", record, log_dir)


def log_order(record: dict, log_dir: str | None = None) -> None:
    """One line per executed order, carrying the full environment snapshot at
    fill time so a trade's context is never reconstructed by joining logs."""
    _append("orders", record, log_dir)
