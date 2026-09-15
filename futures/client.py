from binance.um_futures import UMFutures

import config

client = UMFutures(key=config.api_key, secret=config.api_secret)


def test_connection() -> None:
    """
    Verifies network connectivity to the Binance USDⓈ-M Futures API,
    then confirms the API key itself is valid and authenticated by
    fetching the account's USDT balance.
    """
    client.ping()

    balances = client.balance()
    usdt = next((b for b in balances if b["asset"] == "USDT"), None)

    print("Connected to Binance Futures API.")
    print("USDT balance:", usdt["balance"] if usdt else "N/A")


if __name__ == "__main__":
    test_connection()