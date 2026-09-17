import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "futures"))

import pytest  # noqa: E402  (the path insert above has to come first)

import notify  # noqa: E402


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """No test may send a Telegram message. Applies to the WHOLE suite.

    bot.run() notifies from paths that several tests drive - a failed order,
    a halt - so any test that exercises the loop would otherwise post to the
    operator's real chat with the real credentials out of futures/config.py.
    Both halves are here: the credentials are hidden, so notify short-circuits
    before formatting, and requests.post is replaced, so a test that supplies
    its own credentials still cannot reach the network.

    tests/test_notify.py overrides both with fakes of its own - a module
    fixture is set up after this one."""
    monkeypatch.setattr(notify, "config", None)

    def forbidden(*args, **kwargs):
        raise AssertionError("a test tried to call the network")

    monkeypatch.setattr(notify.requests, "post", forbidden)
