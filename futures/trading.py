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


def place_limit_order_test(symbol: str, side: str, quantity: float, price: float, time_in_force: str = "GTC") -> dict:
    """
    Validates a LIMIT order's parameters and signature without sending it
    to the matching engine or affecting the account.
    """
    return client.new_order_test(
        symbol=symbol,
        side=side,
        type="LIMIT",
        timeInForce=time_in_force,
        quantity=quantity,
        price=price,
    )
