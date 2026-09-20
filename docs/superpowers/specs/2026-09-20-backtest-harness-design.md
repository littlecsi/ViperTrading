# Backtest harness

Date: 2026-09-20
Status: implemented

## Problem

The zone-scaling ladder had never been measured against anything. It was
testnet-validated in the sense that it places orders and does not crash, but
nobody could say whether the strategy makes money, how much risk it takes to
do so, or whether it beats simply holding the asset.

## Design

`backtest/` replays historical 1-minute bars through the **real** decision
modules. `strategy.decide`, `ladder.desired_orders`, `liquidation_scale`,
`trim_scale` and `plan_orders` are imported and called, never reimplemented.
That is what their purity was always for, and it means the harness measures
the shipping logic rather than a copy that drifts from it.

Only the exchange is simulated: the order book, fills, fees, funding and
margin.

| Module | Role |
|---|---|
| `data.py` | Binance public klines + funding history, cached to `backtest/data/` |
| `engine.py` | The simulator: resting orders, fills, position accounting, margin |
| `zones.py` | Out-of-sample zone derivation |
| `report.py` | Metrics and the HTML report |
| `run.py` | Entry point; runs A and B, writes logs and the report |

Run logs mirror the live journal record shapes (`*-ticks.jsonl`,
`*-orders.jsonl`) so backtest and production history are directly comparable
and can feed the same PPO pipeline later.

### Two runs, because one would be misleading

**Run A** uses `futures/test/settings.json` verbatim. Those zones were drawn
by hand after the test period was already visible — the support at 2,371.26
sits just under the period's low — so A carries look-ahead and cannot be read
as a forecast.

**Run B** re-derives zones every 7 days from the trailing 30 days only, by an
explicit percentile rule in `zones.py`. A single out-of-sample derivation was
tried first and was useless: ETH rallied between the two periods, the whole
ladder sat below the test range, and the bot idled for a month. Walk-forward
is the standard answer and it is what the backtesting skill prescribes.

### Costs, all modelled

Maker 2 bps on rung fills, taker 4 bps on market exits, and funding every 8
hours at the rates actually realised. A month-long leveraged long pays real
funding; omitting it is the most common way a perpetuals backtest flatters
itself.

## Three defects found by disbelieving the result

The first version reported **+318.94%** for a month in which ETH moved +10%.
Each of these was found by refusing to accept a number that good.

1. **Same-bar round trips.** Filling every rung the bar's *range* touched let
   a BUY below and a SELL above both fill within one minute, booking the
   bar's full range risk-free. A bar is now treated as one directional sweep
   and only rungs on the dominant side can fill. The book can never
   round-trip against itself inside a bar.
2. **No margin check.** The simulator filled orders the account could never
   have afforded — the position reached 264,696 USDT of notional against a
   25,000 cap. Binance rejects those with `-2019`, the very error CLAUDE.md
   records the live bot hitting at `exposure_fraction: 1.0`. `_place` now
   refuses an order whose initial margin exceeds available balance.
3. **Drawdown computed after downsampling.** Running the peak over hourly
   samples skipped the troughs between them, so the chart bottomed at -21%
   while the table beside it said -36.2%. Peak-tracking now happens at full
   resolution and only the finished curve is thinned.

A fourth, smaller: the window ended at "now", so every run measured a
different month and missed the data cache. It is pinned to the last complete
UTC midnight, and two consecutive runs now produce identical figures.

## Results (21 Aug - 20 Sep 2026, ETHUSDT)

| | Run A (look-ahead) | Run B (walk-forward) | Buy and hold |
|---|---|---|---|
| Return | +21.52% | +54.15% | +13.15% |
| Max drawdown | -23.8% | -36.2% | - |

Read with the limitations below, not as a forecast.

## Limitations

1. One month, one symbol, one regime — ETH ranged and drifted up. A
   zone-scaling ladder lives or dies on regime.
2. The simulated bot decides once a minute; the real one polls every second,
   so the detect-settle-reconcile cycle is coarser here.
3. The intrabar path is unknowable from OHLC; the one-side sweep rule is a
   conservative assumption, not the truth.
4. No order-book depth and no market impact.
5. In run B a single fill is a large share of all realised profit, so its
   headline is one event rather than a repeatable process.
