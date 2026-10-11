"""Source-checkout imports, or explicitly verified installed-wheel imports."""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("ACIRA_TEST_INSTALLED") == "1":
    # Run pytest with --import-mode=importlib from outside the checkout. Never
    # let a release test silently exercise source instead of the shipped wheel.
    for name in ("acira", "acira_cdp_proxy", "acira_dashboard"):
        location = Path(importlib.import_module(name).__file__).resolve()
        if location.is_relative_to(REPO_ROOT) or "site-packages" not in location.parts:
            raise RuntimeError(f"Installed-wheel tests imported {name} from {location}")
elif str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def pytest_configure(config):
    if config.option.basetemp is None:
        # Keep Windows-friendly checkout ACLs without allowing another pytest
        # process to delete an active run's fixtures.
        parent = REPO_ROOT / ".pytest_tmp"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.option.basetemp = parent / ("run-"+uuid.uuid4().hex)


def pytest_sessionfinish(session, exitstatus):
    # Optional integrations may skip during core-only development, but release
    # jobs must exercise both adapters and real-browser checks in full.
    if os.environ.get("ACIRA_RELEASE_TESTS") == "1":
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        if reporter is None or reporter.stats.get("skipped"):
            session.exitstatus = 1
            if reporter is not None:
                reporter.write_line("Acira release verification failed: skipped tests are not accepted")
