"""Entry point: fetch, simulate run A and run B, write logs and the report."""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import data
import engine
import report as report_mod
import zones as zonelib

import env
import market
import settings

RESULTS = Path(__file__).resolve().parent / "results"
SYMBOL = "ETHUSDT"
START_BALANCE = 5_000.0
WINDOW_DAYS = 30

# The exchange filters and margin brackets are properties of the symbol, and
# a backtest must not depend on an account being reachable to re-run. Fetched
# once, cached, and replayed from disk thereafter.
_META = Path(__file__).resolve().parent / "data" / f"meta-{SYMBOL}.json"


def _meta():
    if _META.exists():
        raw = json.loads(_META.read_text())
        return market.Filters(**raw["filters"]), raw["brackets"]

    import client
    api = client.build(env.TEST)
    filters = market.get_filters(api, SYMBOL)
    brackets = market.get_leverage_brackets(api, SYMBOL)
    _META.write_text(json.dumps({"filters": filters.__dict__, "brackets": brackets}))
    return filters, brackets


def _write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def simulate(label, cfg, bars, funding, filters, brackets):
    sim = engine.Simulator(cfg, filters, brackets, START_BALANCE)
    result = sim.run(bars, funding)
    _write_jsonl(RESULTS / f"{label}-ticks.jsonl", result.ticks)
    _write_jsonl(RESULTS / f"{label}-orders.jsonl", result.orders)
    _write_jsonl(RESULTS / f"{label}-equity.jsonl", result.equity)
    return result


def walk_forward(label, base_cfg, bars, funding, filters, brackets,
                 history, lookback_days=30, step_days=7):
    """Run B: zones re-derived periodically, never from future data.

    A single set of out-of-sample zones drawn once, before the window, goes
    stale the moment the market moves - the first attempt put the whole ladder
    below the test period's range and the bot idled for a month, which says
    nothing about the strategy. Walk-forward is the standard answer: at each
    step the zones are rebuilt from the trailing `lookback_days` of data
    ENDING AT THAT MOMENT, so every decision still sees only its own past.

    `history` is the full price series including the pre-window warmup, so a
    re-derivation partway through the test can look back across the boundary
    without ever looking forward."""
    sim = engine.Simulator(base_cfg, filters, brackets, START_BALANCE)
    pending = sorted(funding, key=lambda f: f["time"])
    idx = 0
    step_ms = step_days * 86_400_000
    lookback_ms = lookback_days * 86_400_000
    next_rederive = 0
    derivations = []

    for bar in bars:
        now_ms = bar["open_time"]
        if now_ms >= next_rederive:
            window = [b["close"] for b in history
                      if now_ms - lookback_ms <= b["open_time"] < now_ms]
            if len(window) > len(base_cfg.zones) + 1:
                derived = zonelib.derive(window, count=len(base_cfg.zones))
                sim.set_config(settings.Settings(**{**base_cfg.__dict__, "zones": derived}))
                derivations.append({
                    "time": now_ms,
                    "zones": [[z.support, z.resistance] for z in derived],
                })
            next_rederive = now_ms + step_ms

        rate = None
        while idx < len(pending) and pending[idx]["time"] <= now_ms:
            rate = pending[idx]["rate"]
            idx += 1
        sim.step(bar, rate)

    sim.result.account = sim.acct
    _write_jsonl(RESULTS / f"{label}-ticks.jsonl", sim.result.ticks)
    _write_jsonl(RESULTS / f"{label}-orders.jsonl", sim.result.orders)
    _write_jsonl(RESULTS / f"{label}-equity.jsonl", sim.result.equity)
    _write_jsonl(RESULTS / f"{label}-zones.jsonl", derivations)
    sim.result.derivations = derivations
    return sim.result


def summarise(label, result, rejected=0):
    acct = result.account
    final = result.equity[-1]["equity"]
    return {
        "label": label,
        "orders": len(result.orders),
        "realized": acct.realized,
        "fees": acct.fees,
        "funding": acct.funding_paid,
        "start_equity": START_BALANCE,
        "final_equity": final,
        "return_pct": (final / START_BALANCE - 1) * 100,
        "max_notional": max(abs(e["notional"]) for e in result.equity),
        "rejected_orders": rejected,
    }


def _window_end() -> int:
    """The last complete UTC midnight, in epoch ms.

    Pinned rather than "now" for two reasons. A window ending at the current
    instant measures a slightly different month on every run - the same
    backtest reported +39.44% and then +47.47% minutes apart, which makes the
    number impossible to cite or to compare against a later change. It also
    misses the data cache on every run, since the cache is keyed on the
    window, so each run re-downloaded 43,200 bars it already had."""
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def main():
    now = _window_end()
    start = now - WINDOW_DAYS * 86_400_000
    warmup_start = start - WINDOW_DAYS * 86_400_000

    print(f"window : {datetime.fromtimestamp(start/1000, timezone.utc)} -> "
          f"{datetime.fromtimestamp(now/1000, timezone.utc)}")

    bars = data.fetch_klines(SYMBOL, start, now)
    funding = data.fetch_funding(SYMBOL, start, now)
    warmup = data.fetch_klines(SYMBOL, warmup_start, start)
    history = warmup + bars
    filters, brackets = _meta()
    print(f"bars   : {len(bars)}   funding: {len(funding)}   warmup: {len(warmup)}")

    configured = settings.load(env.settings_path(env.TEST))
    buy_hold = (bars[-1]["close"] / bars[0]["close"] - 1) * 100

    summaries = []

    print("\n--- A: configured zones (contains look-ahead) ---")
    for z in configured.zones:
        print(f"  zone {z.support:.2f} - {z.resistance:.2f}")
    sim_a = engine.Simulator(configured, filters, brackets, START_BALANCE)
    result_a = sim_a.run(bars, funding)
    _write_jsonl(RESULTS / "A-configured-ticks.jsonl", result_a.ticks)
    _write_jsonl(RESULTS / "A-configured-orders.jsonl", result_a.orders)
    _write_jsonl(RESULTS / "A-configured-equity.jsonl", result_a.equity)
    summaries.append(summarise("A-configured", result_a, sim_a.rejected))

    print("\n--- B: walk-forward zones (no look-ahead) ---")
    result_b = walk_forward("B-walkforward", configured, bars, funding,
                            filters, brackets, history)
    summaries.append(summarise("B-walkforward", result_b, 0))
    print(f"  zone sets derived: {len(result_b.derivations)}")

    for s in summaries:
        print(f"\n{s['label']}")
        print(f"  orders     : {s['orders']}")
        print(f"  realized   : {s['realized']:+,.2f}")
        print(f"  fees       : {s['fees']:,.2f}")
        print(f"  funding    : {s['funding']:+,.2f}")
        print(f"  max notional: {s['max_notional']:,.2f}")
        print(f"  final eq   : {s['final_equity']:,.2f}  ({s['return_pct']:+.2f}%)")

    meta = {
        "symbol": SYMBOL,
        "window_start": start,
        "window_end": now,
        "bars": len(bars),
        "start_balance": START_BALANCE,
        "buy_and_hold_pct": buy_hold,
        "first_price": bars[0]["close"],
        "last_price": bars[-1]["close"],
        "runs": summaries,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (RESULTS / "summary.json").write_text(json.dumps(meta, indent=2))

    stats, path = report_mod.build(meta, {
        "A-configured": {"equity": result_a.equity, "orders": result_a.orders},
        "B-walkforward": {"equity": result_b.equity, "orders": result_b.orders},
    })
    print(f"report: {path}")
    print(f"\nbuy and hold: {buy_hold:+.2f}%")
    return meta


if __name__ == "__main__":
    main()
