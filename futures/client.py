import sys
import time

import binance.api
from binance.um_futures import UMFutures

import config
import env

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


def sync_time(mode: str) -> int:
    """Align outgoing request timestamps with Binance's clock.

    Binance rejects signed requests whose timestamp runs ahead of its server,
    and this machine's clock does exactly that. The connector offers no offset
    hook, so the offset is measured once against a public endpoint and applied
    to every timestamp thereafter.

    Patches binance.api rather than binance.lib.utils: api.py imports
    get_timestamp by name, so rebinding it in the source module would not
    affect the code that actually runs."""
    probe = UMFutures(base_url=TESTNET_URL) if env.is_testnet(mode) else UMFutures()
    offset = _measure_offset(probe)
    binance.api.get_timestamp = lambda: int(time.time() * 1000) + offset
    return offset


def build(mode: str) -> UMFutures:
    """Construct a Futures client for one mode's account.

    `mode` is env.TEST or env.LIVE, and env.is_testnet decides from it - which
    means an unrecognised mode raises here rather than falling through to the
    live branch. That ordering is the point: the default-shaped mistake in a
    two-account selector is the one that reaches real money.

    Testnet and live use entirely separate credentials. They are not
    interchangeable, and this is the only place either pair is read."""
    sync_time(mode)
    if env.is_testnet(mode):
        return UMFutures(
            key=config.testnet_key,
            secret=config.testnet_secret,
            base_url=TESTNET_URL,
        )
    return UMFutures(key=config.api_key, secret=config.api_secret)


def check_connection(mode: str) -> None:
    """Operator smoke check: credentials work, clock is synced, balance reads.

    Placing no orders, it is safe to run against either account - which is why
    it takes the mode explicitly instead of defaulting. Reading a live balance
    should be a thing you asked for."""
    api = build(mode)
    api.ping()
    balances = api.balance()
    usdt = next((b for b in balances if b["asset"] == "USDT"), None)
    print(f"Connected to Binance Futures ({env.label(mode)}).")
    print("USDT balance:", usdt["balance"] if usdt else "N/A")


if __name__ == "__main__":
    # Defaults to testnet: running this file with no argument must never be
    # the thing that touches the live account.
    check_connection(sys.argv[1] if len(sys.argv) > 1 else env.TEST)
