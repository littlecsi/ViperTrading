"""Rebuilding order-journal rows for fills the loop never saw.

Fill detection is poll-based by design: _detect_fill diffs the tracked
rung->id map against get_open_orders once a tick, so a rung that leaves the
book is noticed up to one poll_seconds late. That map lives in memory and dies
with the process. A fill that lands between the last poll and the process
ending - a deploy, a crash, a reboot, a power cut - is therefore never
detected, and nothing afterwards asks the exchange what became of it. On the
next start the position is simply adopted by the reconciliation guard, leaving
the order journal showing a position that appeared from nowhere. That journal
is both the audit trail for real money and the dataset a PPO policy trains on,
and neither use survives a hole like that.

This module is the pure half of the fix: trades in, journal records out. No
I/O, no client, no clock - the same rule strategy.py and ladder.py follow, and
for the same reason. bot.py reads the journal, calls the exchange, and passes
the results in; everything that decides WHICH fills are missing is here, where
it can be tested exhaustively against fabricated trades.

Two mistakes matter more than any other, and they pull in opposite directions:

  Writing a fill twice silently corrupts the dataset - the same trade appears
  as two, and nothing downstream can tell. Hence known_ids.

  Writing a fill this bot never placed injects somebody else's trade into its
  audit trail. The account has history predating the bot. Hence the anchor,
  which never extends past evidence that the bot was actually running.

When the two conflict, prefer silence: a missing row can be reconstructed by
hand from the exchange later, an invented one cannot be found at all.
"""

MAX_LOOKBACK_DAYS = 7

# Slack applied to the anchor, and it only ever looks FURTHER back.
#
# A live order row carries the moment it was JOURNALLED, which is up to one
# poll_seconds plus a round-trip after the fill it describes. Anchoring exactly
# there can step over a second fill that landed inside that gap and was never
# detected - the precise case this module exists for. Reaching back further
# costs nothing because known_ids drops anything already recorded, while
# reaching back too little loses a row permanently. The asymmetry decides it.
SAFETY_MARGIN_MS = 60_000

_DAY_MS = 86_400_000

# Present on every recovered row, always null. The bot was not running when
# these filled, so none of it is knowable after the fact. They are written
# rather than omitted so a recovered row has the same SHAPE as a live one -
# a consumer reading the journal should not have to special-case which keys
# exist, only read `recovered` to know how much to trust the context.
_UNKNOWABLE = ("reason", "rung_price", "mark_price", "active_zone_index",
               "trend", "leverage", "balance")


def anchor_ms(last_order_ms, last_tick_ms, now_ms,
              max_lookback_days=MAX_LOOKBACK_DAYS,
              safety_margin_ms=SAFETY_MARGIN_MS):
    """The instant recovery looks back to, or None to recover nothing.

    Three sources, in descending order of precision:

      The last fill already in the order journal. Exact: everything after it
      is by definition not yet recorded.

      Failing that, the last tick record. The order journal is empty on a
      fresh install and on any run that never filled, but the tick journal
      still says when this bot was last provably alive - which is precisely
      the window in which it could have had orders working.

      Failing both, None. No evidence the bot ever ran here means every trade
      on the account belongs to somebody else, so nothing is recovered.

    The result is then floored to `max_lookback_days` ago. That bounds both the
    exchange query and the journal read, and it errs toward missing an old fill
    rather than inventing one: after a longer outage than the cap, the earliest
    fills are left alone rather than swept in alongside whatever else the
    operator did on the account in the meantime."""
    candidate = last_order_ms if last_order_ms is not None else last_tick_ms
    if candidate is None:
        return None
    candidate -= safety_margin_ms
    floor = now_ms - max_lookback_days * _DAY_MS
    return max(candidate, floor)


def _aggregate(trades):
    """Group raw userTrades rows into one entry per orderId.

    The exchange reports FILLS, and one order can fill as several of them at
    several prices. The live path writes one line per ORDER (query_order hands
    back avgPrice and cumQty already aggregated), so recovery matches that
    shape - a per-trade row here would make recovered history inconsistent with
    everything written while the bot was up.

    Quantities and costs sum; price is recovered from them as a notional-
    weighted average, never as a mean of the trade prices, which would be wrong
    for any order whose legs differed in size."""
    orders = {}
    for t in trades:
        entry = orders.setdefault(
            int(t["orderId"]),
            {"qty": 0.0, "notional": 0.0, "commission": 0.0, "realized": 0.0,
             "side": t.get("side"), "asset": t.get("commissionAsset"),
             "maker": True, "time": 0, "ids": []},
        )
        entry["qty"] += float(t["qty"])
        entry["notional"] += float(t["quoteQty"])
        entry["commission"] += float(t.get("commission") or 0.0)
        entry["realized"] += float(t.get("realizedPnl") or 0.0)
        # An order can fill partly as maker and partly as taker; claiming maker
        # for the whole of it would misstate the fee basis.
        entry["maker"] = entry["maker"] and bool(t.get("maker"))
        entry["time"] = max(entry["time"], int(t["time"]))
        entry["ids"].append(int(t["id"]))
    return orders


def records_for(trades, known_ids, recovered_at):
    """Order-journal records for every filled order not already journalled.

    `known_ids` is the set of order ids the journal already carries; anything
    in it is skipped, which is what makes running this on every single startup
    safe. Oldest first, because the journal is append-only and read as a
    timeline."""
    records = []
    for order_id, agg in _aggregate(trades).items():
        if order_id in known_ids:
            continue
        if agg["qty"] <= 0:
            # Defensive: an average price is undefined here, and letting a
            # ZeroDivisionError out would abort the batch and lose every other
            # record in it too.
            continue
        record = {
            "order_id": order_id,
            "symbol": trades[0].get("symbol") if trades else None,
            "side": agg["side"],
            "quantity": agg["qty"],
            "executed_qty": agg["qty"],
            "fill_price": agg["notional"] / agg["qty"],
            "notional": agg["notional"],
            # Live rows leave commission null - the loop deliberately does not
            # spend a userTrades call per fill. Recovery is already reading
            # userTrades, so these rows can carry what that call knows.
            "commission": agg["commission"],
            "commission_asset": agg["asset"],
            "realized_pnl": agg["realized"],
            "maker": agg["maker"],
            "trade_ids": sorted(agg["ids"]),
            "fill_time": agg["time"],
            "recovered": True,
            "recovered_at": recovered_at,
        }
        record.update({key: None for key in _UNKNOWABLE})
        records.append(record)

    records.sort(key=lambda r: r["fill_time"])
    return records
