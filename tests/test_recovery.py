"""Startup fill recovery (futures/recovery.py).

The gap this closes: fill detection is poll-based, so a rung that fills between
the last _detect_fill poll and the process dying is never seen. The rung->id
map lives in memory and dies with it, and on restart the position is simply
adopted - leaving the order journal, which is both the audit trail for real
money and the dataset a PPO policy trains on, showing a position that appeared
from nowhere.

Everything here is pure: trades in, records out. The two facts it must never
get wrong are (1) never write a fill twice, because a duplicated row corrupts
the dataset silently, and (2) never sweep in trades this bot did not place -
the account has history predating it.
"""

import pytest

import recovery


DAY_MS = 86_400_000


def trade(order_id, qty, price, trade_id=1, time=1_000, side="BUY",
          commission="0.1", realized="0", maker=True):
    """One userTrades row, in the string-typed shape the exchange returns."""
    return {
        "symbol": "ETHUSDT",
        "id": trade_id,
        "orderId": order_id,
        "side": side,
        "price": str(price),
        "qty": str(qty),
        "quoteQty": str(round(float(qty) * float(price), 8)),
        "commission": commission,
        "commissionAsset": "USDT",
        "realizedPnl": realized,
        "time": time,
        "maker": maker,
    }


# --- the anchor: how far back recovery looks -------------------------------


def test_anchor_prefers_the_last_journalled_fill():
    """An order journal with entries is the most precise anchor there is."""
    assert recovery.anchor_ms(5_000, 3_000, now_ms=9_000, safety_margin_ms=0) == 5_000


def test_anchor_falls_back_to_the_tick_journal():
    """No order journal yet - a fresh install, or one that has never filled.

    The tick journal says when the bot was last provably alive, which bounds
    the window in which it could have had orders working."""
    assert recovery.anchor_ms(None, 3_000, now_ms=9_000, safety_margin_ms=0) == 3_000


def test_anchor_is_none_with_no_journal_at_all():
    """A truly fresh account recovers NOTHING. Fails safe: with no evidence
    this bot ever ran, every trade on the account belongs to somebody else."""
    assert recovery.anchor_ms(None, None, now_ms=9_000, safety_margin_ms=0) is None


def test_anchor_is_floored_to_the_lookback_cap():
    """After a long outage the window is capped.

    This errs toward MISSING an old fill rather than INVENTING one: a gap in
    the journal is recoverable by hand, a fabricated row in the PPO dataset is
    not."""
    now = 100 * DAY_MS
    stale = now - 30 * DAY_MS
    assert recovery.anchor_ms(stale, None, now_ms=now, max_lookback_days=7) == now - 7 * DAY_MS


def test_the_cap_never_pushes_a_recent_anchor_backwards():
    now = 100 * DAY_MS
    recent = now - 10 * 60_000
    assert recovery.anchor_ms(
        recent, None, now_ms=now, max_lookback_days=7, safety_margin_ms=0
    ) == recent


def test_the_anchor_reaches_back_past_the_last_journalled_fill():
    """A live order row is stamped when it was JOURNALLED, up to a poll after
    the fill. Anchoring exactly there steps over a second fill that landed in
    that gap and was never detected - the very case this module exists for.
    Reaching further back is free: known_ids drops anything already recorded."""
    now = 100 * DAY_MS
    journalled = now - 10 * 60_000
    anchor = recovery.anchor_ms(journalled, None, now_ms=now)

    assert anchor == journalled - recovery.SAFETY_MARGIN_MS
    assert anchor < journalled, "the margin must only ever look FURTHER back"


# --- which trades become records -------------------------------------------


def test_no_trades_means_no_records():
    assert recovery.records_for([], known_ids=set(), recovered_at="now") == []


def test_a_trade_already_in_the_journal_is_not_written_again():
    """The restart-after-restart case. Recovery runs on EVERY startup, so an
    already-known order must produce nothing at all."""
    trades = [trade(order_id=111, qty="1.0", price="2000")]
    assert recovery.records_for(trades, known_ids={111}, recovered_at="now") == []


def test_an_unknown_trade_becomes_one_record():
    trades = [trade(order_id=111, qty="0.458", price="2625", trade_id=42, time=1_700)]
    [rec] = recovery.records_for(trades, known_ids=set(), recovered_at="2026-09-20T00:00:00Z")

    assert rec["order_id"] == 111
    assert rec["side"] == "BUY"
    assert rec["quantity"] == pytest.approx(0.458)
    assert rec["executed_qty"] == pytest.approx(0.458)
    assert rec["fill_price"] == pytest.approx(2625.0)
    assert rec["notional"] == pytest.approx(0.458 * 2625.0)
    assert rec["trade_ids"] == [42]
    assert rec["fill_time"] == 1_700


def test_known_and_unknown_are_separated():
    trades = [
        trade(order_id=111, qty="1.0", price="2000", trade_id=1),
        trade(order_id=222, qty="2.0", price="2100", trade_id=2),
    ]
    recs = recovery.records_for(trades, known_ids={111}, recovered_at="now")
    assert [r["order_id"] for r in recs] == [222]


# --- aggregation: one order can fill as several trades ---------------------


def test_one_order_filled_across_several_trades_becomes_one_record():
    """Observed on the real exchange: order 16795863483 came back as two rows.

    The live path writes one line per ORDER (query_order's avgPrice/cumQty), so
    recovery aggregates to match. A per-trade row here would make the dataset
    inconsistent with every record written while the bot was running."""
    trades = [
        trade(order_id=99, qty="1.0", price="100", trade_id=1, time=500, commission="0.1"),
        trade(order_id=99, qty="3.0", price="200", trade_id=2, time=900, commission="0.3"),
    ]
    [rec] = recovery.records_for(trades, known_ids=set(), recovered_at="now")

    assert rec["quantity"] == pytest.approx(4.0)
    # Weighted by notional, not a mean of the two prices: (100 + 600) / 4
    assert rec["fill_price"] == pytest.approx(175.0)
    assert rec["notional"] == pytest.approx(700.0)
    assert rec["commission"] == pytest.approx(0.4)
    assert rec["trade_ids"] == [1, 2]
    assert rec["fill_time"] == 900  # the moment the order finished filling


def test_realized_pnl_sums_across_an_orders_trades():
    trades = [
        trade(order_id=7, qty="1", price="100", trade_id=1, side="SELL", realized="10.5"),
        trade(order_id=7, qty="1", price="100", trade_id=2, side="SELL", realized="4.5"),
    ]
    [rec] = recovery.records_for(trades, known_ids=set(), recovered_at="now")
    assert rec["realized_pnl"] == pytest.approx(15.0)
    assert rec["side"] == "SELL"


def test_maker_is_true_only_when_every_trade_was_a_maker():
    """A single order can fill partly as maker and partly as taker. Claiming
    maker for the whole order would misstate the fee basis."""
    mixed = [
        trade(order_id=8, qty="1", price="100", trade_id=1, maker=True),
        trade(order_id=8, qty="1", price="100", trade_id=2, maker=False),
    ]
    [rec] = recovery.records_for(mixed, known_ids=set(), recovered_at="now")
    assert rec["maker"] is False

    all_maker = [
        trade(order_id=9, qty="1", price="100", trade_id=1, maker=True),
        trade(order_id=9, qty="1", price="100", trade_id=2, maker=True),
    ]
    [rec] = recovery.records_for(all_maker, known_ids=set(), recovered_at="now")
    assert rec["maker"] is True


def test_records_come_back_oldest_first():
    """The journal is append-only and read as a timeline; writing a later fill
    above an earlier one would misrepresent the order of events."""
    trades = [
        trade(order_id=3, qty="1", price="100", trade_id=3, time=3_000),
        trade(order_id=1, qty="1", price="100", trade_id=1, time=1_000),
        trade(order_id=2, qty="1", price="100", trade_id=2, time=2_000),
    ]
    recs = recovery.records_for(trades, known_ids=set(), recovered_at="now")
    assert [r["fill_time"] for r in recs] == [1_000, 2_000, 3_000]


def test_a_zero_quantity_order_is_skipped_rather_than_dividing_by_zero():
    """Defensive. A zero-qty row should not exist, but an average price is
    undefined for one and a ZeroDivisionError here would abort the whole
    recovery - and with it every other record in the batch."""
    trades = [
        trade(order_id=1, qty="0", price="100", trade_id=1),
        trade(order_id=2, qty="1", price="100", trade_id=2),
    ]
    recs = recovery.records_for(trades, known_ids=set(), recovered_at="now")
    assert [r["order_id"] for r in recs] == [2]


# --- honesty about what a recovered record cannot know ---------------------


def test_a_recovered_record_is_marked_and_admits_what_it_cannot_know():
    """The bot was not running when this filled, so the decision context is
    genuinely unknown. Writing startup-time values into those fields would
    dress a guess up as a fact - and this row feeds PPO training."""
    trades = [trade(order_id=111, qty="1", price="100")]
    [rec] = recovery.records_for(trades, known_ids=set(), recovered_at="2026-09-20T00:00:00Z")

    assert rec["recovered"] is True
    assert rec["recovered_at"] == "2026-09-20T00:00:00Z"
    for unknowable in ("reason", "rung_price", "mark_price", "active_zone_index",
                       "trend", "leverage", "balance"):
        assert unknowable in rec, f"{unknowable} must be present so the shape matches live rows"
        assert rec[unknowable] is None, f"{unknowable} is not knowable after the fact"


def test_recovered_rows_carry_the_commission_live_rows_omit():
    """The live path deliberately skips the userTrades call, so its records
    leave commission null. Recovery is already reading userTrades, so these
    rows can carry it - see the CLAUDE.md note on that gap."""
    trades = [trade(order_id=111, qty="1", price="100", commission="0.24045")]
    [rec] = recovery.records_for(trades, known_ids=set(), recovered_at="now")
    assert rec["commission"] == pytest.approx(0.24045)
    assert rec["commission_asset"] == "USDT"
