# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

ViperTrading is a small automated crypto trading bot for Binance. It implements a volatility breakout strategy: each day it computes a target price from the previous day's OHLC data, buys the configured asset (default `XRP`, quoted in `USDT`) once the current price crosses the target, and sells the full position shortly after UTC midnight if the price has since dropped below the newly computed target. Trade and debug events are reported to Slack via incoming webhooks/API calls.

All code lives in `binance/`:
- `binance/main.py` — entry point; runs the infinite trading loop (`while True`, polling every 2 seconds via `time.sleep(2)`), tracks `balance` and `target_price` across iterations, and re-derives the target price once per day at the UTC day boundary.
- `binance/biat.py` — all trading logic and helpers: balance/price lookups, target-price calculation (volatility breakout formula), buy/sell order placement, and the Slack `post_message` helper.

## Setup

Dependencies are pinned in `requirements.txt`. Install them with:

```
pip install -r requirements.txt
```

A `binance/config.py` module is required at runtime but is not checked into the repo (it's gitignored). It must define:
- `api_key`, `api_secret` — Binance API credentials
- `slack_token` — Slack bot token used by `post_message`

The bot posts to hardcoded Slack channels: `#trade-alert` (buy/sell/nothing-to-sell notifications), `#target` (daily target price), and `#debug` (exceptions from failed API calls).

## Running

```
python binance/main.py
```

There is no test suite, linter, or build step configured in this repo.

## Architecture notes

- `main.py` holds all mutable trading state (`balance`, `target_price`) in module-level variables inside the loop; `biat.py` functions are stateless and always take `client`/values as arguments and return computed results — don't reintroduce global state into `biat.py`.
- Every Binance API call in `biat.py` is wrapped in a bare `try/except` that reports failures to the `#debug` Slack channel rather than raising — the buy/sell functions are the exception, they re-raise after reporting so the main loop can be interrupted on a failed order.
- Asset symbols are normalized by appending `"USDT"` whenever the input symbol is 5 characters or fewer (e.g. `"XRP"` -> `"XRPUSDT"`). This convention is repeated in every function that takes an `asset` argument — keep it consistent if you add new functions.
- The target price (volatility breakout) formula: `target = close_yesterday + (high_yesterday - low_yesterday) * 0.5`, using `get_ytd_ohlcv`, which calls `client.klines(asset, "1d", endTime=<today's UTC midnight in ms>)` and takes the last daily candle before today.
- The day-rollover check in `main.py` (`mid < now < mid + timedelta(seconds=10)`) is a narrow 10-second window evaluated once per 2-second poll — be careful when changing the poll interval or this window, since widening the interval risks skipping the rollover check entirely.
