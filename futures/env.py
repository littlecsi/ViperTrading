"""Which exchange, which settings file, which journal - resolved from one mode
string.

The mode is set in bot.py as a module constant, NOT in settings.json. That is
the whole point of this module existing. settings.json is re-read every tick,
so a selector living there can be changed under a running bot holding a
leveraged position; bot.py is read once at import, so changing environments
requires a restart as a matter of physics rather than as a rule some guard has
to enforce.

Pure: no I/O, no client, no clock, and deliberately no `import config`. Keeping
credentials out of here is what lets this module - and any test of it - be
imported on a fresh clone, where futures/config.py does not exist because it is
gitignored. client.py already imports config and is where the credential pair
is chosen; see client.build()."""

from pathlib import Path

TEST = "test"
LIVE = "live"
MODES = (TEST, LIVE)

_ROOT = Path(__file__).resolve().parent


def validate(mode):
    """Return `mode` if it names an environment, else raise.

    Fails CLOSED, and every accessor below routes through it. The tempting
    shape - `return mode == TEST` for is_testnet, letting anything else mean
    live - silently resolves a typo to the real-money account. A misspelled
    mode must stop the process at startup, before a client is built and before
    an order can exist."""
    if not isinstance(mode, str) or mode not in MODES:
        valid = " or ".join(repr(m) for m in MODES)
        raise ValueError(f"mode must be {valid}, got {mode!r}")
    return mode


def is_testnet(mode) -> bool:
    """Whether this mode trades Binance's testnet. The one place the old
    `testnet` boolean survives, now derived rather than configured."""
    return validate(mode) == TEST


def label(mode) -> str:
    """Display name for banners, journal records and Telegram messages.

    ASCII only: this reaches print() in bot.py's startup banner, which runs
    before the loop's error handling exists, on a console whose codepage is
    cp949 and cannot encode anything else."""
    return "TESTNET" if is_testnet(mode) else "LIVE"


def settings_path(mode) -> str:
    """This mode's settings.json. Separate files because the two environments
    want genuinely different risk parameters against genuinely different wallet
    sizes - not one file whose numbers get edited back and forth."""
    return str(_ROOT / validate(mode) / "settings.json")


def log_dir(mode) -> str:
    """This mode's journal directory.

    Separate because the tick journal is both the audit trail for real money
    and the dataset a PPO policy trains against. A testnet fill written into
    the live journal corrupts both, and nothing in a record says which exchange
    produced it."""
    return str(_ROOT / validate(mode) / "logs")
