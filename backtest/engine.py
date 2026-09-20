"""The simulator: replays 1m bars through the REAL decision code.

What is simulated here is only the exchange - the book, fills, fees, funding
and margin. Every decision is made by futures/strategy.py and futures/ladder.py
themselves, imported and called, never reimplemented. That is what their purity
was always for: decide() and desired_orders() take plain values and return
plain values, so a backtest can drive them exactly as bot.py does. A
reimplementation here would test a copy of the strategy rather than the
strategy, and would drift from it silently the first time either changed.

Fidelity limits, stated because a backtest that hides them is worse than none:

  The simulated bot decides ONCE A MINUTE; the real one polls every second.
  Inside a bar only OHLC is known, so the ladder's detect/settle/reconcile
  cycle is coarser here than in production. This cuts both ways.

  The intrabar path is unknown. If a bar's range spans several rungs they all
  fill, at their own prices; the real sequence might have been different, and
  a reconcile might have intervened partway.

  There is no book depth and no market impact. At this size on ETHUSDT that is
  a fair approximation, but it is an approximation.

A resting order fills only when the bar trades THROUGH it, never on a touch -
at your own limit price you are behind everyone already queued there, and
touch-fills are the single most common way a backtest flatters a maker
strategy.
"""

from dataclasses import dataclass, field

import execution
import ladder
import market
import strategy
from settings import LONG

# Binance USD-M futures, standard VIP 0 tier.
MAKER_FEE = 0.0002   # resting ladder rungs
TAKER_FEE = 0.0004   # market exits: HALT_FLATTEN, STOP_OUT, dead-band SCALE_OUT

_EXITS = (strategy.STOP_OUT, strategy.HALT_FLATTEN)


@dataclass
class Resting:
    id: int
    side: str
    price: float
    qty: float


@dataclass
class Account:
    """Isolated-margin position state, the subset the strategy actually reads."""
    wallet: float
    position: float = 0.0        # signed quantity, + long / - short
    entry: float = 0.0
    isolated: float = 0.0        # margin committed to the open position
    realized: float = 0.0
    fees: float = 0.0
    funding_paid: float = 0.0

    @property
    def notional(self) -> float:
        return self.position * self.entry


@dataclass
class Result:
    ticks: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    equity: list = field(default_factory=list)
    account: Account = None


def _liquidation_price(acct: Account, leverage: int, tier, trend: str) -> float:
    """Where the exchange would close this position.

    Same formula ladder.liquidation_scale projects with - margin balance meets
    maintenance margin - solved for price rather than for a scale factor. Kept
    consistent with it deliberately: if the two disagreed, the cap would be
    protecting against a liquidation level the account never actually had."""
    qty = abs(acct.position)
    if qty == 0:
        return 0.0
    mmr, maint = tier.maint_margin_rate, tier.maint_amount
    if trend == LONG:
        return (acct.entry * qty - acct.isolated - maint) / (qty * (1 - mmr))
    return (acct.entry * qty + acct.isolated + maint) / (qty * (1 + mmr))


def _apply_fill(acct: Account, side: str, qty: float, price: float,
                leverage: int, fee_rate: float) -> dict:
    """Move the position and settle the cash consequences of one fill."""
    signed = qty if side == "BUY" else -qty
    fee = qty * price * fee_rate
    acct.fees += fee
    acct.wallet -= fee

    before = acct.position
    realized = 0.0

    if before == 0 or (before > 0) == (signed > 0):
        # Opening or adding: the average entry moves toward the new fill.
        total = abs(before) + qty
        acct.entry = (acct.entry * abs(before) + price * qty) / total
        acct.isolated += qty * price / leverage
        acct.position = before + signed
    else:
        # Reducing, closing, or flipping through zero.
        closing = min(qty, abs(before))
        direction = 1.0 if before > 0 else -1.0
        realized = (price - acct.entry) * closing * direction
        acct.realized += realized
        acct.wallet += realized
        # Margin is released in proportion to the share of the position closed.
        acct.isolated -= acct.isolated * (closing / abs(before))
        acct.position = before + signed

        remainder = qty - closing
        if remainder > 0:
            # Flipped: the far side opens fresh at this price.
            acct.entry = price
            acct.isolated = remainder * price / leverage
        elif acct.position == 0:
            acct.entry = 0.0
            acct.isolated = 0.0

    return {"side": side, "quantity": qty, "fill_price": price,
            "notional": qty * price, "commission": fee, "realized_pnl": realized}


class Simulator:
    def __init__(self, cfg, filters, brackets, start_balance: float):
        self.cfg = cfg
        self.filters = filters
        self.brackets = brackets
        self.acct = Account(wallet=start_balance)
        self.resting: dict[int, Resting] = {}
        self.active_index = None
        self.halted = False
        self.settling = False
        self._next_id = 1
        self.rejected = 0
        self.result = Result()

    # --- exchange side -----------------------------------------------------

    def _committed_margin(self) -> float:
        """Margin already spoken for: the open position, plus every resting
        order that would ADD to it if filled.

        A trim-side order is excluded - filling it releases margin rather than
        consuming it."""
        pending = sum(
            o.qty * o.price / self.cfg.leverage
            for o in self.resting.values()
            if (o.side == "BUY") == (self.cfg.trend == LONG)
        )
        return self.acct.isolated + pending

    def _place(self, side: str, price: float, qty: float) -> bool:
        """Rest one order, unless the exchange would refuse it for margin.

        This check is not decoration. Without it the simulator happily fills
        orders the account could never have afforded: the first version let the
        position reach 264,696 USDT of notional against a 25,000 cap and
        reported +400% for the month. Binance rejects an order whose initial
        margin exceeds available balance with -2019 - the very error CLAUDE.md
        records the live bot hitting at exposure_fraction 1.0 - and a backtest
        that does not model the rejection is not modelling the same strategy.

        A refused order is dropped, not retried, which is what the live loop
        does with a rejection too."""
        adds = (side == "BUY") == (self.cfg.trend == LONG)
        if adds:
            required = qty * price / self.cfg.leverage
            if self._committed_margin() + required > self.acct.wallet:
                self.rejected += 1
                return False
        self.resting[self._next_id] = Resting(self._next_id, side, price, qty)
        self._next_id += 1
        return True

    def _cancel_all(self) -> None:
        self.resting.clear()

    def _fills_for(self, bar) -> list:
        """Resting orders the bar traded THROUGH - on ONE side only.

        The one-side rule is the whole integrity of this backtest, so it is
        worth being explicit about what it prevents.

        A 1m bar reports only OHLC. Filling every rung the bar's RANGE touched
        would let a BUY below and a SELL above both fill within the same
        minute, booking the bar's full range as profit with no risk and no
        regard for the order in which price actually visited those levels.
        For a grid strategy that single assumption manufactures most of the
        return: the first version of this engine reported +318% over a month
        in which ETH moved +10%, and essentially all of it came from here.

        Since the intrabar path is unknowable from OHLC, the bar is treated as
        ONE directional sweep. Whichever excursion from the open was larger is
        taken as the move that happened, and only rungs on that side can fill,
        in the order price would have reached them. A bar can still sweep many
        rungs - a real move should - but it can never round-trip against
        itself.

        This is deliberately conservative. Real price does reverse inside a
        minute, so some genuine fills are missed. Missing a fill understates
        the result; inventing one overstates it, and an overstatement is what
        gets acted on."""
        up, down = bar["high"] - bar["open"], bar["open"] - bar["low"]
        if up == down == 0:
            return []

        if up > down:
            # The rise dominated: price worked upward through the sell side.
            hit = [o for o in self.resting.values()
                   if o.side == "SELL" and bar["high"] > o.price]
            hit.sort(key=lambda o: o.price)
        else:
            hit = [o for o in self.resting.values()
                   if o.side == "BUY" and bar["low"] < o.price]
            hit.sort(key=lambda o: o.price, reverse=True)
        return hit

    def _funding(self, rate: float, mark: float) -> None:
        """A perpetual charges the position holder every 8 hours."""
        if self.acct.position == 0:
            return
        payment = self.acct.position * mark * rate
        self.acct.wallet -= payment
        self.acct.funding_paid += payment

    def set_config(self, cfg) -> None:
        """Swap the running configuration, tearing the ladder down first.

        Mirrors bot.py's reload path: a change to `zones` cancels the resting
        rungs against the OLD config, because that is where they are priced,
        and nothing else would ever re-price them. Used by the walk-forward
        run, which re-derives zones periodically."""
        if tuple(cfg.zones) != tuple(self.cfg.zones):
            self._cancel_all()
            self.settling = False
            self.active_index = None
        self.cfg = cfg

    # --- the tick ----------------------------------------------------------

    def _snapshot(self, mark: float) -> market.Snapshot:
        tier = market.maintenance_tier_from(self.brackets, abs(self.acct.position) * mark)
        unrealized = (mark - self.acct.entry) * self.acct.position if self.acct.position else 0.0
        return market.Snapshot(
            mark_price=mark,
            position_amt=self.acct.position,
            unrealized_pnl=unrealized,
            wallet_balance=self.acct.wallet,
            available_balance=self.acct.wallet - self.acct.isolated,
            entry_price=self.acct.entry,
            isolated_wallet=self.acct.isolated,
            liquidation_price=_liquidation_price(self.acct, self.cfg.leverage, tier, self.cfg.trend),
        )

    def _ladder_orders(self, decision, snapshot, mark) -> tuple:
        """The rung set bot.py would place, through the SAME two caps it uses."""
        cfg = self.cfg
        zone = cfg.zones[decision.zone_index]
        desired = ladder.desired_orders(
            zone, cfg.trend, decision.max_n, cfg.alpha, cfg.rung_spacing_pct, mark)

        survival = ladder.survival_price(
            cfg.zones, decision.zone_index, cfg.trend, cfg.liquidation_buffer_pct)
        tier = market.maintenance_tier_from(self.brackets, abs(snapshot.position_amt) * mark)

        accumulate = [o for o in desired
                      if (o.side == "BUY") == (cfg.trend == LONG)]
        delta_notional = sum(o.size for o in accumulate)
        delta_qty = sum(o.size / o.price for o in accumulate if o.price)

        scale = ladder.liquidation_scale(
            snapshot.position_amt, snapshot.entry_price, snapshot.isolated_wallet,
            cfg.leverage, tier, survival, delta_qty, delta_notional, cfg.trend)
        desired = ladder.apply_liquidation_cap(desired, scale, cfg.trend)

        # The trim side is capped against the position that REALLY exists, or a
        # fresh activation rests sells sized for a position it has not built.
        trim = ladder.trim_scale(desired, snapshot.position_amt, cfg.trend)
        desired = ladder.apply_trim_cap(desired, trim, cfg.trend)

        valid, _deferred = ladder.validate_orders(desired, mark, cfg.trend, self.filters)
        return valid, scale, trim

    def step(self, bar, funding_rate=None) -> None:
        cfg = self.cfg
        mark = bar["close"]

        # 1. Fills first: the book was working before this bar's decision.
        filled = self._fills_for(bar)
        for order in filled:
            rec = _apply_fill(self.acct, order.side, order.qty, order.price,
                              cfg.leverage, MAKER_FEE)
            rec.update({"order_id": order.id, "symbol": cfg.symbol,
                        "reason": "ladder_fill", "rung_price": order.price,
                        "mark_price": mark, "trend": cfg.trend,
                        "leverage": cfg.leverage, "maker": True,
                        "fill_time": bar["open_time"],
                        "balance": self.acct.wallet})
            self.result.orders.append(rec)
            del self.resting[order.id]

        # 2. Funding, charged on the position this bar opened with.
        if funding_rate is not None:
            self._funding(funding_rate, mark)

        snapshot = self._snapshot(mark)
        decision = strategy.decide(
            price=mark, zones=cfg.zones, trend=cfg.trend,
            position_notional=snapshot.position_amt * mark,
            wallet_balance=snapshot.wallet_balance, leverage=cfg.leverage,
            alpha=cfg.alpha, exposure_fraction=cfg.exposure_fraction,
            stop_buffer=cfg.stop_buffer, rebalance_threshold=cfg.rebalance_threshold,
            min_notional=self.filters.min_notional, active_index=self.active_index,
        )

        zone_changed = decision.zone_index != self.active_index
        in_zone_ladder = (decision.zone_index is not None
                          and decision.reason in (strategy.SCALE_IN, strategy.SCALE_OUT))

        if decision.reason in _EXITS or (decision.reason == strategy.SCALE_OUT
                                         and decision.zone_index is None):
            # Every exit is a market order, exactly as in bot.py: certainty of
            # execution beats price when the position must change size NOW.
            self._cancel_all()
            qty = execution.quantity_for(abs(decision.delta), mark, self.filters)
            side = "BUY" if decision.delta > 0 else "SELL"
            adds = (side == "BUY") == (cfg.trend == LONG)
            affordable = (not adds) or (
                self.acct.isolated + qty * mark / cfg.leverage <= self.acct.wallet)
            if execution.is_executable(qty, mark, self.filters) and affordable:
                rec = _apply_fill(self.acct, side, qty, mark, cfg.leverage, TAKER_FEE)
                rec.update({"order_id": None, "symbol": cfg.symbol,
                            "reason": decision.reason, "mark_price": mark,
                            "trend": cfg.trend, "leverage": cfg.leverage,
                            "maker": False, "fill_time": bar["open_time"],
                            "balance": self.acct.wallet})
                self.result.orders.append(rec)
            self.settling = False
        elif in_zone_ladder:
            if zone_changed:
                self._cancel_all()
                self.settling = False
            if not self.resting:
                valid, _s, _t = self._ladder_orders(decision, snapshot, mark)
                for o in valid:
                    self._place(o.side, o.price, o.size / o.price)
            elif filled:
                # A fill happened: wait for the move to finish before repricing,
                # rather than rebuilding mid-cascade once per poll.
                self.settling = True
            elif self.settling:
                valid, _s, _t = self._ladder_orders(decision, snapshot, mark)
                book = [{"orderId": o.id, "side": o.side, "price": str(o.price),
                         "origQty": str(o.qty)} for o in self.resting.values()]
                plan = ladder.plan_orders(valid, book, self.filters)
                for order_id in plan.cancel:
                    self.resting.pop(order_id, None)
                for o in plan.place:
                    self._place(o.side, o.price, o.size / o.price)
                self.settling = False

        self.active_index = decision.zone_index

        equity = self.acct.wallet + snapshot.unrealized_pnl
        self.result.equity.append({
            "time": bar["open_time"], "price": mark, "equity": equity,
            "wallet": self.acct.wallet, "position": self.acct.position,
            "notional": self.acct.position * mark,
            "unrealized": snapshot.unrealized_pnl,
            "zone": decision.zone_index, "resting": len(self.resting),
        })
        self.result.ticks.append({
            "time": bar["open_time"], "action": decision.action,
            "reason": decision.reason, "price": mark,
            "active_zone_index": decision.zone_index, "d": decision.d,
            "target_notional": decision.target_signed,
            "position_before": snapshot.position_amt, "balance": self.acct.wallet,
        })

    def run(self, bars, funding) -> Result:
        pending = sorted(funding, key=lambda f: f["time"])
        idx = 0
        for bar in bars:
            rate = None
            while idx < len(pending) and pending[idx]["time"] <= bar["open_time"]:
                rate = pending[idx]["rate"]
                idx += 1
            self.step(bar, rate)
        self.result.account = self.acct
        return self.result
