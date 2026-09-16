from dataclasses import dataclass


@dataclass(frozen=True)
class Filters:
    step_size: float
    min_qty: float
    min_notional: float


def get_filters(client, symbol: str) -> Filters:
    info = client.exchange_info()
    entry = next(s for s in info["symbols"] if s["symbol"] == symbol)

    step_size = min_qty = min_notional = 0.0
    for f in entry["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
            min_qty = float(f["minQty"])
        elif f["filterType"] == "MIN_NOTIONAL":
            min_notional = float(f["notional"])

    return Filters(step_size=step_size, min_qty=min_qty, min_notional=min_notional)


def get_mark_price(client, symbol: str) -> float:
    return float(client.ticker_price(symbol)["price"])


def _balance_entry(client, asset: str):
    return next((b for b in client.balance() if b["asset"] == asset), None)


def get_wallet_balance(client, asset: str = "USDT") -> float:
    """Total wallet balance. Position sizing must use this rather than
    available balance, which shrinks as margin is consumed."""
    entry = _balance_entry(client, asset)
    return float(entry["balance"]) if entry else 0.0


def get_available_balance(client, asset: str = "USDT") -> float:
    entry = _balance_entry(client, asset)
    return float(entry["availableBalance"]) if entry else 0.0


def _position_entry(client, symbol: str):
    positions = client.get_position_risk(symbol=symbol)
    return next((p for p in positions if p["symbol"] == symbol), None)


def get_position_amt(client, symbol: str) -> float:
    """Signed position size in contracts: positive long, negative short."""
    entry = _position_entry(client, symbol)
    return float(entry["positionAmt"]) if entry else 0.0


def get_unrealized_pnl(client, symbol: str) -> float:
    entry = _position_entry(client, symbol)
    return float(entry["unRealizedProfit"]) if entry else 0.0


def set_leverage(client, symbol: str, leverage: int) -> None:
    client.change_leverage(symbol=symbol, leverage=leverage)
