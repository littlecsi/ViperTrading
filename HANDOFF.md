# Handoff notes

## Where things stand (2026-09-16, `develop` branch)

The zone-scaling Futures bot is **implemented and testnet-validated**:

- `futures/settings.py` + `futures/settings.json` — validated, hot-reloaded runtime configuration
  (symbol, trend, leverage, alpha, exposure fraction, rebalance threshold, stop buffer, poll interval,
  testnet flag, zone ladder).
- `futures/client.py` — builds the `UMFutures` client for either live or testnet credentials, and
  corrects for this machine's clock running ahead of Binance's server clock so signed requests aren't
  rejected.
- `futures/market.py`, `futures/execution.py`, `futures/journal.py` — exchange reads, order
  quantity/filter handling, and JSONL tick/order logging.
- `futures/strategy.py` — the pure zone-scaling decision logic.
- `futures/bot.py` — the polling loop tying all of the above together. **This is the entry point:**
  `python futures/bot.py`.
- 58 tests under `tests/`, all passing.
- The bot has placed a **real order on Binance testnet** — a full decide -> size -> execute -> journal
  cycle has run end-to-end against the live testnet exchange, not just in tests.

`futures/trading.py` (the earlier scratch trading module) and `futures/test.py` (the earlier personal
scratch script) are both **gone** — superseded by the modules above.

Old Spot-based code in `binance/` is legacy and out of scope; the Futures bot in `futures/` is the only
active system.

### Next step

**Extended testnet observation before any live run.** The bot has only been exercised for short,
supervised windows so far. Before pointing it at live trading, let it run against testnet unattended
for a longer period and confirm it behaves correctly across multiple ticks, zone transitions, and
settings reloads without unhandled exceptions.

### Known items to carry forward

- **The live (non-testnet) Binance account holds a pre-existing open position unrelated to this bot.**
  Do not point the bot at live (`"testnet": false` in `settings.json`) until that position has been
  reconciled — the bot's position-sizing logic assumes it owns the entire position on the configured
  symbol.
- **Testnet position sizes are larger than the original design assumed.** The testnet wallet holds
  ~5000 USDT; with `leverage: 5` and `exposure_fraction: 1.0` in the current `settings.json`, the
  maximum position is `5000 * 5 * 1.0` = ~$25,000 notional — five times the $1,000 wallet the original
  design discussion assumed. This is expected behavior (sizing correctly reads the live wallet balance),
  but keep it in mind when judging whether a testnet fill "looks too big."

## To work from home (new environment)

1. `git pull` on the `develop` branch to get this code.
2. Create the virtual environment (named `.viper`, gitignored so it won't come with the pull):
   ```
   python -m venv .viper
   ```
   Activate it, then install dependencies (this now also installs `pytest`):
   ```
   pip install -r requirements.txt
   ```
3. Recreate `futures/config.py` manually — it's gitignored and does **not** come through git. It now
   needs **four** values (live credentials plus separate testnet credentials):
   ```python
   api_key = "..."
   api_secret = "..."
   testnet_key = "..."
   testnet_secret = "..."
   ```
   Use the same Binance Futures API keys from this session (pull them from wherever you saved them —
   they were only ever written to this file, not committed anywhere). Live and testnet keys are not
   interchangeable.
4. Verify the setup:
   ```
   python futures/client.py
   ```
   Should print your USDT balance (testnet by default) if the keys/connection are good.
5. Run the test suite to confirm the environment is sound:
   ```
   .viper/Scripts/python.exe -m pytest tests/ -v
   ```
   All 58 tests should pass.
6. **Do not run `python futures/bot.py`** unless you intend to place real orders — even on testnet, it
   trades against a live (test) exchange, not a simulation.
