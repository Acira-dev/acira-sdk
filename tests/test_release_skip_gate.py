"""Release jobs must not treat missing optional/browser checks as a pass."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("release_conftest", Path(__file__).with_name("conftest.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.mark.parametrize("enabled,skipped,reporter_available,expected", [
    (True, True, True, 1), (True, False, True, 0),
    (True, False, False, 1), (False, True, True, 0),
])
def test_release_only_skip_gate(monkeypatch, enabled, skipped, reporter_available, expected):
    monkeypatch.setenv("ACIRA_RELEASE_TESTS", "1" if enabled else "0")
    reporter = SimpleNamespace(stats={"skipped": [object()]} if skipped else {}, write_line=lambda message: None)
    session = SimpleNamespace(exitstatus=0, config=SimpleNamespace(pluginmanager=SimpleNamespace(
        getplugin=lambda name: reporter if reporter_available else None)))
    gate.pytest_sessionfinish(session, 0)
    assert session.exitstatus == expected
