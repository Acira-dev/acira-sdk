"""Source-checkout imports, or explicitly verified installed-wheel imports."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

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
@pytest.fixture
def select_capture_target():
    async def select(page, target):
        box = None
        try:
            await page.locator(".acira-capture-shield[data-ready='true']").wait_for(state="visible")
            await target.scroll_into_view_if_needed()
            box = await target.bounding_box()
            assert box and box["width"] > 0 and box["height"] > 0
            identity = await target.evaluate("el => ({id:el.id, href:el.getAttribute('href')})")
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await page.wait_for_function("""identity => {
              const raw=document.querySelector('#acira-capture-facts')?.textContent;
              if (!raw) return false;
              const fp=JSON.parse(raw);
              return identity.id ? fp.id===identity.id : fp.href===identity.href;
            }""", arg=identity)
        except Exception as error:
            # Observe only a failed original selection. Never retry input, extend
            # its timeout, or persist page text, target values, URLs or tokens.
            try:
                state = await asyncio.wait_for(page.evaluate("""box => {
                  const shield=document.querySelector('.acira-capture-shield');
                  const r=shield?.getBoundingClientRect();
                  return {shield_present:Boolean(shield),
                    shield_ready:shield?.dataset.ready==='true',
                    shield_hidden:shield?.hidden??null,
                    shield_bounds:r?{x:r.x,y:r.y,width:r.width,height:r.height}:null,
                    viewport:{width:innerWidth,height:innerHeight},
                    point_tags:box?document.elementsFromPoint(box.x+box.width/2,box.y+box.height/2)
                      .slice(0,4).map(el=>el.tagName):[],
                    fingerprint_present:Boolean(document.querySelector('#acira-capture-facts')?.textContent)};
                }""", box), 1)
                error.add_note("Acira capture geometry: " + json.dumps(state, sort_keys=True))
            except Exception as diagnostic_error:
                error.add_note("Acira capture geometry unavailable: " + type(diagnostic_error).__name__)
            raise
    return select
