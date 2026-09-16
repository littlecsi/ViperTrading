import time

import binance.api
from binance.um_futures import UMFutures

import config

TESTNET_URL = "https://testnet.binancefuture.com"


def _measure_offset(probe: UMFutures, samples: int = 5) -> int:
    """Median of (server time - local midpoint) in milliseconds.

    Uses the midpoint of the local clock readings taken either side of the
    call so that network latency cancels out instead of biasing the offset."""
    offsets = []
    for _ in range(samples):
        before = int(time.time() * 1000)
        server = probe.time()["serverTime"]
        after = int(time.time() * 1000)
        offsets.append(server - (before + after) // 2)
    return sorted(offsets)[len(offsets) // 2]


def sync_time(testnet: bool) -> int:
    """Align outgoing request timestamps with Binance's clock.

    Binance rejects signed requests whose timestamp runs ahead of its server,
    and this machine's clock does exactly that. The connector offers no offset
    hook, so the offset is measured once against a public endpoint and applied
    to every timestamp thereafter.

    Patches binance.api rather than binance.lib.utils: api.py imports
    get_timestamp by name, so rebinding it in the source module would not
    affect the code that actually runs."""
    probe = UMFutures(base_url=TESTNET_URL) if testnet else UMFutures()
    offset = _measure_offset(probe)
    binance.api.get_timestamp = lambda: int(time.time() * 1000) + offset
    return offset


def build(testnet: bool) -> UMFutures:
    """Construct a Futures client. Testnet uses entirely separate
    credentials from live — they are not interchangeable."""
    sync_time(testnet)
    if testnet:
        return UMFutures(
            key=config.testnet_key,
            secret=config.testnet_secret,
            base_url=TESTNET_URL,
        )
    return UMFutures(key=config.api_key, secret=config.api_secret)


def test_connection(testnet: bool = True) -> None:
    client = build(testnet)
    client.ping()
    balances = client.balance()
    usdt = next((b for b in balances if b["asset"] == "USDT"), None)
    label = "testnet" if testnet else "live"
    print(f"Connected to Binance Futures ({label}).")
    print("USDT balance:", usdt["balance"] if usdt else "N/A")


if __name__ == "__main__":
    test_connection()
