"""Shared test setup.

The suite must be hermetic: a test that reaches the real network passes
or fails depending on where it runs and on what a third-party service
happens to hold that day. Any outbound request is therefore an error,
and a test that wants network behaviour stubs the specific call it needs.
"""

import pytest

import requests


class NetworkUsedInTest(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly on any un-stubbed HTTP call.

    This caught a real bug: the album tests never stubbed the lyrics
    fetch, so on a runner with network they hit LRCLIB for real and wrote
    unexpected .lrc sidecars into the asserted library listing.
    """
    def blocked(*args, **kwargs):
        target = args[0] if args else kwargs.get("url", "?")
        raise NetworkUsedInTest(
            "test made a real HTTP request to %s - stub it instead" % (target,))

    for name in ("get", "post", "put", "delete", "head", "request"):
        monkeypatch.setattr(requests, name, blocked)
    monkeypatch.setattr(requests.Session, "request", blocked)
