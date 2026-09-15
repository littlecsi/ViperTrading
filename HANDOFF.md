# Handoff notes

## Where I left off (2026-09-15, `develop` branch)

- Scaffolded a Binance USDⓈ-M Futures client in `futures/`:
  - `futures/config.py` — holds `api_key`/`api_secret` (gitignored, not synced — see below).
  - `futures/client.py` — shared `UMFutures` client + `test_connection()` (pings the API, then confirms the key is authenticated via `client.balance()`).
  - `futures/trading.py` — `get_balance(asset)`, `place_limit_order(...)`, `place_limit_order_test(...)` (dry-run via Binance's `/order/test` endpoint — validates params/filters without touching the account or matching engine).
  - `futures/test.py` — personal scratch script (gitignored) for manually exercising `trading.py`. Currently prints USDT balance and runs a test limit order (`BTCUSDT BUY 0.01 @ 10000`).
- Old Spot-based code in `binance/` is legacy and being ignored — the Futures rebuild in `futures/` is the active work.
- Nothing beyond this has been built yet: no market/stop orders, no strategy logic, no Omen-facing API layer.

## To work from home (new environment)

1. `git pull` on the `develop` branch to get this code.
2. Create the virtual environment (named `.viper`, gitignored so it won't come with the pull):
   ```
   python -m venv .viper
   ```
   Activate it, then install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Recreate `futures/config.py` manually — it's gitignored and does **not** come through git. It needs:
   ```python
   api_key = "..."
   api_secret = "..."
   ```
   Use the same Binance Futures API key/secret from this session (pull them from wherever you saved them — they were only ever written to this file, not committed anywhere).
4. Verify the setup:
   ```
   python futures/client.py
   ```
   Should print your USDT balance if the key/connection are good.
5. `futures/test.py` is also gitignored (matches the `test.py` pattern), so it won't transfer either — recreate it if you want the same scratch script, or write your own.
