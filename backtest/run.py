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
# Every window the report covers, longest last. One month is the smoke test;
# six months and a year are what say whether the strategy survives a change of
# regime rather than one friendly tape.
WINDOWS = (("1 month", 30), ("6 months", 180), ("1 year", 365))
WARMUP_DAYS = 30          # trailing data run B derives its first zones from
WINDOW_DAYS = max(days for _, days in WINDOWS)

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
    end = _window_end()
    earliest = end - (WINDOW_DAYS + WARMUP_DAYS) * 86_400_000

    # ONE fetch covering the longest span plus warmup; every window slices from
    # it. Fetching per window would re-download the overlap three times over -
    # a year of one-minute bars is 525,600 of them.
    print(f"fetching {WINDOW_DAYS + WARMUP_DAYS} days of 1m bars "
          f"(ending {datetime.fromtimestamp(end/1000, timezone.utc):%Y-%m-%d})...")
    history = data.fetch_klines(SYMBOL, earliest, end)
    all_funding = data.fetch_funding(SYMBOL, earliest, end)
    filters, brackets = _meta()
    print(f"  {len(history):,} bars, {len(all_funding)} funding events")

    configured = settings.load(env.settings_path(env.TEST))

    # The control: the same ladder without the newly added 1411 support. Adding
    # a support does not only add a zone - it moves where past_adverse_end
    # decides the thesis is dead, so it is a change to the STOP, not just to
    # the sizing curve. Running both is the only way to say what it bought.
    without_1411 = settings.Settings(**{
        **configured.__dict__,
        "zones": tuple(z for z in configured.zones if z.support != 1411.0),
    })

    halt_new = min(z.support for z in configured.zones) * (1 - configured.stop_buffer)
    halt_old = min(z.support for z in without_1411.zones) * (1 - configured.stop_buffer)
    print(f"\nHALT threshold  with 1411: {halt_new:,.2f}   without: {halt_old:,.2f}")
    print("\nzone ladder under test:")
    for z in configured.zones:
        print(f"  {z.support:>8.2f} - {z.resistance:<8.2f}")

    windows = {}
    for name, days in WINDOWS:
        start = end - days * 86_400_000
        bars = [b for b in history if b["open_time"] >= start]
        funding = [f for f in all_funding if f["time"] >= start]
        slug = name.replace(" ", "")
        print(f"\n=== {name} ({len(bars):,} bars) ===")

        runs = {}
        for label, cfg in (("With 1411", configured), ("Without 1411", without_1411)):
            sim = engine.Simulator(cfg, filters, brackets, START_BALANCE)
            res = sim.run(bars, funding)
            _write_jsonl(RESULTS / f"{slug}-{label.replace(' ','')}-ticks.jsonl", res.ticks)
            _write_jsonl(RESULTS / f"{slug}-{label.replace(' ','')}-orders.jsonl", res.orders)
            runs[label] = {"equity": res.equity, "orders": res.orders, "_r": res}

        res_b = walk_forward(f"{slug}-walkforward", configured, bars, funding,
                             filters, brackets, history)
        runs["Walk-forward"] = {"equity": res_b.equity, "orders": res_b.orders, "_r": res_b}

        hold = (bars[-1]["close"] / bars[0]["close"] - 1) * 100
        for label, r in runs.items():
            res = r["_r"]
            final = res.equity[-1]["equity"]
            low = min(e["equity"] for e in res.equity)
            print(f"  {label:14}: {final:>10,.2f}  ({(final/START_BALANCE-1)*100:+8.2f}%)  "
                  f"low {low:>9,.2f}  fills {len(res.orders):>5}  "
                  f"fees {res.account.fees:>9,.2f}")
        print(f"  {'hold':14}: {hold:+8.2f}%   ETH {bars[0]['close']:.0f} -> {bars[-1]['close']:.0f}"
              f"  (low {min(b['low'] for b in bars):.0f})")

        for r in runs.values():
            r.pop("_r")

        windows[name] = {
            "days": days, "start": start, "end": end, "bars": len(bars),
            "buy_and_hold_pct": hold,
            "first_price": bars[0]["close"],
            "last_price": bars[-1]["close"],
            "low_price": min(b["low"] for b in bars),
            "zone_sets": len(res_b.derivations),
            "runs": runs,
        }

    meta = {
        "symbol": SYMBOL,
        "start_balance": START_BALANCE,
        "zones": [[z.support, z.resistance] for z in configured.zones],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_end": end,
    }
    stats, path = report_mod.build_multi(meta, windows)
    (RESULTS / "summary.json").write_text(json.dumps(
        {"meta": meta, "stats": stats}, indent=2))
    print(f"\nreport: {path}")
    return stats


if __name__ == "__main__":
    main()
