from client import client


def get_balance(asset: str = "USDT") -> float:
    """
    Returns the available balance for a specific asset in the Futures account.
    """
    balances = client.balance()

    for entry in balances:
        if entry["asset"] == asset:
            return float(entry["availableBalance"])

    return 0.0


def place_limit_order(symbol: str, side: str, quantity: float, price: float, time_in_force: str = "GTC") -> dict:
    """
    Places a LIMIT order on the Futures market.
    """
    return client.new_order(
        symbol=symbol,
        side=side,
        type="LIMIT",
        timeInForce=time_in_force,
        quantity=quantity,
        price=price,
    )
