"""Invalid framework timeout settings must never disable Acira's hang bound."""
import asyncio

import pytest

from acira.adapters.browser_use import _browser_use_action_timeout
from acira_cdp_proxy.pause import wait_with_review_pause


@pytest.mark.parametrize("setting,override", [
    (None, None), ("", None), ("invalid", None), ("nan", float("nan")),
    ("inf", float("inf")), ("-1", -1), ("0", 0), (None, True),
])
def test_invalid_action_timeout_keeps_finite_default(monkeypatch, setting, override):
    if setting is None:
        monkeypatch.delenv("BROWSER_USE_ACTION_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("BROWSER_USE_ACTION_TIMEOUT_S", setting)
    assert _browser_use_action_timeout(override) == 180.0


@pytest.mark.asyncio
async def test_action_wait_honors_environment_and_per_call_bounds(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_ACTION_TIMEOUT_S", "0.01")
    for override in (None, 0.02):
        with pytest.raises(TimeoutError):
            await wait_with_review_pause(asyncio.sleep(10), _browser_use_action_timeout(override))
    assert _browser_use_action_timeout(0.02) == 0.02
