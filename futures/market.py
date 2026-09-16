from dataclasses import dataclass


@dataclass(frozen=True)
class Filters:
    step_size: float
    min_qty: float
    min_notional: float


def get_filters(client, symbol: str) -> Filters:
    info = client.exchange_info()
    entry = next(s for s in info["symbols"] if s["symbol"] == symbol)

    step_size = min_qty = min_notional = None
    for f in entry["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
            min_qty = float(f["minQty"])
        elif f["filterType"] == "MIN_NOTIONAL":
            min_notional = float(f["notional"])

    # Defaulting an unresolved filter to zero turns both safety limits into no
    # limit: a zero step_size divides by zero when sizing, and a zero
    # min_notional lets the churn guard and the executable check pass anything.
    # A missing filter must be fatal at startup, not a silent degrade.
    for name, value in (
        ("LOT_SIZE", step_size),
        ("LOT_SIZE", min_qty),
        ("MIN_NOTIONAL", min_notional),
    ):
        if value is None or value <= 0:
            raise ValueError(f"{symbol}: missing {name} filter")

    return Filters(step_size=step_size, min_qty=min_qty, min_notional=min_notional)


@dataclass(frozen=True)
class Snapshot:
    """Everything one tick needs about the market and the account, read at a
    single instant."""

    mark_price: float
    position_amt: float
    unrealized_pnl: float
    wallet_balance: float
    available_balance: float


# --- pure extractors -------------------------------------------------------
# These take an already-fetched payload so that one response can feed several
# values. They do no I/O, which is what lets a tick hit each endpoint once.


def _balance_entry(balances, asset: str):
    return next((b for b in balances if b["asset"] == asset), None)


def wallet_balance_from(balances, asset: str = "USDT") -> float:
    """Total wallet balance, from the `balance` field. Position sizing must use
    this rather than `availableBalance`, which shrinks as margin is consumed by
    an open position and would stall accumulation short of its target size."""
    entry = _balance_entry(balances, asset)
    return float(entry["balance"]) if entry else 0.0


def available_balance_from(balances, asset: str = "USDT") -> float:
    """Free margin. Logged for visibility only - never feeds position sizing."""
    entry = _balance_entry(balances, asset)
    return float(entry["availableBalance"]) if entry else 0.0


def _position_entry(positions, symbol: str):
    # A symbol the account has never traded is simply absent from the payload;
    # that is a position of zero, not an error.
    return next((p for p in positions if p["symbol"] == symbol), None)


def position_amt_from(positions, symbol: str) -> float:
    """Signed position size in contracts: positive long, negative short. The
    sign arrives as part of the string (e.g. "-1.250") and must survive."""
    entry = _position_entry(positions, symbol)
    return float(entry["positionAmt"]) if entry else 0.0


def unrealized_pnl_from(positions, symbol: str) -> float:
    entry = _position_entry(positions, symbol)
    return float(entry["unRealizedProfit"]) if entry else 0.0


# --- exchange reads --------------------------------------------------------


def get_snapshot(client, symbol: str, asset: str = "USDT") -> Snapshot:
    """One REST call per endpoint: 3 calls, 11 request weight.

    At a one-second poll interval the old five-getter tick cost 21 weight
    (1260/min against a 2400/min limit) because /balance and /positionRisk were
    each fetched twice. Beyond the rate-limit headroom, deriving every field
    from one response per endpoint makes the tick a real snapshot: price,
    position and balance can no longer disagree because they were read at
    different instants."""
    price = float(client.ticker_price(symbol)["price"])  # weight 1
    positions = client.get_position_risk(symbol=symbol)  # weight 5
    balances = client.balance()  # weight 5

    return Snapshot(
        mark_price=price,
        position_amt=position_amt_from(positions, symbol),
        unrealized_pnl=unrealized_pnl_from(positions, symbol),
        wallet_balance=wallet_balance_from(balances, asset),
        available_balance=available_balance_from(balances, asset),
    )


def get_position_amt(client, symbol: str) -> float:
    """Signed position size for one symbol on its own.

    Kept separate from get_snapshot for the reads that are not about the
    configured symbol's current tick: the symbol-change refusal check, which
    asks about the OLD symbol, and the post-order position re-read."""
    return position_amt_from(client.get_position_risk(symbol=symbol), symbol)


def set_leverage(client, symbol: str, leverage: int) -> None:
    client.change_leverage(symbol=symbol, leverage=leverage)
