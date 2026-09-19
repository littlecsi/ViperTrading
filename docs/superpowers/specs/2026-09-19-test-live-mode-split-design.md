# Test/live mode split

Date: 2026-09-19
Status: approved, ready for implementation

## Problem

The bot selects its exchange account with `"testnet": true` in `futures/settings.json` — a
hot-reloaded file. That is the wrong place for the single most consequential switch in the
system, for three reasons.

**It is reachable from a file the loop re-reads every tick.** Because a mid-flight account
swap would abandon an open position on one exchange while trading another, `bot.py` carries a
guard (lines 903-928) whose only job is to refuse a reload that changes `testnet`, and
`notify.py` carries a message branch (`testnet_change_requires_restart`) to explain the
refusal. Both exist to defend a capability nothing wanted.

**Both environments share one journal.** `journal.LOG_DIR` is `futures/logs/`, so testnet ticks
and live ticks land in the same `ticks-YYYY-MM-DD.jsonl` and `orders-YYYY-MM-DD.jsonl`. The
tick journal is the audit trail for real money *and* the dataset a PPO policy is intended to
train on. Mixing fabricated testnet fills into it corrupts both uses, and nothing in a record
says which exchange produced it.

**Both environments share one settings file.** Testnet and live want different risk
parameters — the testnet wallet holds ~5000 USDT against a live wallet of a different size
entirely — but there is one `settings.json`, so moving between environments means editing risk
numbers by hand and remembering to edit them back.

## Goal

New strategy work is validated against testnet, then promoted to live, with:

- one switch, in code, that cannot be changed by editing a hot-reloaded file;
- one copy of the trading logic, so test and live behaviour cannot drift;
- separate settings and separate journals per environment.

## Non-goals

- Running both modes in one process. One process, one mode, chosen at startup.
- Simulation or paper trading. `test` means Binance's testnet exchange — a real exchange with
  real order matching and fake money — not a local simulator.
- Deciding when the bot is ready for live trading. `HANDOFF.md` requires extended unattended
  testnet observation first; this change is groundwork for that decision, not the decision.

## Design

### Layout

```
futures/
  bot.py          MODE = "test"          <-- the one switch
  env.py          NEW: mode -> paths, labels. Pure.
  client.py       build(mode)
  settings.py     load(path); no testnet field
  market.py strategy.py ladder.py execution.py journal.py notify.py   (shared, unchanged
                                                                       except as noted)
  config.py       unchanged, gitignored, four keys + telegram
  test/
    settings.json
    logs/         gitignored
  live/
    settings.json
    logs/         gitignored
```

One copy of every trading module. The directories hold only what legitimately differs per
environment: configuration and journals. No code is duplicated, so a fix made while testing is
the same code that later runs live — there is no promotion step that can be forgotten.

### `env.py`

A new pure module resolving a mode string to the facts that depend on it:

```python
TEST = "test"
LIVE = "live"
MODES = (TEST, LIVE)

def validate(mode) -> str        # returns mode, or raises ValueError
def is_testnet(mode) -> bool
def label(mode) -> str           # "TESTNET" / "LIVE", for banners and messages
def settings_path(mode) -> str   # futures/<mode>/settings.json
def log_dir(mode) -> str         # futures/<mode>/logs
```

Two properties matter more than the contents.

**It does not import `config.py`.** Credential selection stays in `client.py`, which already
imports `config`. `config.py` is gitignored, so a module that imports it cannot be tested on a
fresh clone — the breakage commit `7a6d4a0` had to fix in the notify tests. `env.py` must stay
importable without credentials present.

**`validate()` fails closed.** An unknown or misspelled mode raises before any client is built.
It never falls back to a default, and specifically never to `live`. `"tets"` must stop the
process, not quietly trade real money.

### The switch

```python
MODE = "test"
```

A module constant at the top of `bot.py`, validated at the start of `run()`. Because `bot.py`
is not hot-reloaded, changing environments now *requires* a restart as a matter of physics
rather than as a rule a guard enforces. The guard at `bot.py:903-928` and the
`testnet_change_requires_restart` branch in `notify.format_config_refused` both become
unreachable and are deleted.

### Per-module changes

| Module | Change |
|---|---|
| `settings.py` | Drop `testnet` from `Settings` and `load()`. Remove the `SETTINGS_PATH` default so `path` is required — no caller can silently read a stale top-level file. |
| `client.py` | `build(mode)` and `sync_time(mode)`, selecting the credential pair via `env.is_testnet(mode)`. The `__main__` connectivity check takes a mode on argv, defaulting to `test`. |
| `journal.py` | Unchanged. `LOG_DIR` remains the fallback default for callers that pass nothing. |
| `bot.py` | `MODE` constant; `settings.load(env.settings_path(MODE))` at startup and on every reload; `client.build(MODE)`; mode banner; delete the reload-refusal guard; thread `log_dir` through every journal call. |
| `notify.py` | `format_startup_refused` reads `record["mode"]` rather than `record["testnet"]`. Delete the `testnet_change_requires_restart` branch. Add `format_startup` and a `startup()` entry point. |

### The log directory

CLAUDE.md forbids module-level mutable state in `journal.py`, so the directory is **not**
installed by reassigning `journal.LOG_DIR` at startup. It is threaded explicitly:
`TickLog.__init__(log_dir=...)`, a `log_dir` parameter on `_log_config_reloaded`,
`_log_liquidation_brake` and `_journal_ladder_fill`, and on the inline `journal.log_tick` /
`journal.log_order` calls inside `run()`.

This costs a safety net that must be restored in the same change. `tests/conftest.py`
guarantees no test can write to the operator's journal by patching `journal.LOG_DIR`. Once
`bot.run()` passes an explicit directory, that patch no longer covers it, and tests that drive
`run()` would append fabricated orders to `futures/test/logs/`. The autouse fixture therefore
also patches `env.log_dir` to `tmp_path`. Both halves are needed: the `journal.LOG_DIR` patch
covers callers that pass nothing, the `env.log_dir` patch covers callers that resolve a path.

### Live mode

`MODE = "live"` prints an ASCII banner — cp949-safe, per CLAUDE.md's console constraint — and
pushes a Telegram startup notice, then proceeds:

```
============================================================
  V I P E R  --  L I V E  M O D E
  REAL MONEY. Orders go to the live Binance account.
  ETHUSDT @ 5x  trend=long  max notional 25000.00 USDT
============================================================
```

No typed confirmation. A crash-restart or supervisor relaunch must come back up unattended
while a leveraged position is open; a prompt would block forever at exactly the wrong moment.

The startup notice is pushed in both modes, naming the mode. Knowing a process came up is
useful either way, and only the live banner is loud. It obeys the existing notification rules:
it cannot raise, it cannot stall the loop, and it disappears when unconfigured.

The startup reconciliation guard (`bot.py:815-850`) is unchanged and still runs first. Per
`HANDOFF.md:41-44` the live account holds an unrelated open position, so that guard is expected
to refuse the first live start. That is correct behaviour.

## Migration

- `git mv futures/settings.json futures/test/settings.json`, stripping the `"testnet"` key.
- `futures/live/settings.json` created as a copy of the same values. Live risk parameters are
  not invented here: it lands with `exposure_fraction: 1.0`, the full-size setting, for the
  operator to tune before any live run.
- `.gitignore` gains `futures/*/logs/` alongside the existing `futures/logs/`.

## Testing

New `tests/test_env.py`, covering: every valid mode resolves; an unknown mode raises; the
failure is closed (no fallback to `live`); paths differ per mode and sit under `futures/`.

Updated: `test_settings.py` (drop `testnet` from the fixture and assertions), `test_notify.py`
(the `mode` field in startup-refused, removal of the refusal-branch test, a startup-format
test, `lambda testnet: api` -> `lambda mode: api` at line 704), `test_tick_throttle.py`
(same client-build stub at line 439).

Written test-first, per the repository's TDD workflow.

## Risks

**A committed `MODE = "live"` goes live on whatever machine pulls it.** Accepted, with the loud
banner and Telegram push as the mitigation. An arming file or environment variable was
considered and declined as one more thing to remember on a new machine.

**Live mode has never been exercised.** This change makes live mode reachable and correct; it
does not make it validated. The extended unattended testnet soak in `HANDOFF.md:34-37` remains
a prerequisite for any real-money run.
