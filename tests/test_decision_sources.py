"""The DECISION_SOURCES enum must track guard.py's real decision paths (PR1).

We cannot statically infer which source string each ``return`` assigns (that tagging lands in
PR2). What we CAN do — and what protects the pinned schema — is fail the build whenever the
set of decision-construction sites in guard.py changes. A new site forces a reviewer to decide
whether DECISION_SOURCES needs a new entry and a SCHEMA_VERSION bump.
"""

from __future__ import annotations

import ast
from pathlib import Path

import acira_cdp_proxy.guard
from acira_cdp_proxy.audit_schema import DECISION_SOURCES

GUARD = Path(acira_cdp_proxy.guard.__file__)

# Functions/helpers that finalize a guard verdict.
_DECISION_CTORS = {"CDPPolicyDecision", "blocked_result", "_known_secret_payload_block"}

# Pinned snapshot of decision-construction sites in guard.py. If this changes, review whether
# a new decision_source is needed, then update DECISION_SOURCES (+ SCHEMA_VERSION) and this
# number in the same change.
# Invalid live policy configuration uses the existing security_lockdown source.
# Uncontracted native dialog acceptance is an explicit core prohibition, using
# the existing explicit_rule audit source; no schema enum or field is changed.
EXPECTED_DECISION_SITES = 27


def _decision_site_count() -> int:
    tree = ast.parse(GUARD.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in _DECISION_CTORS:
                count += 1
    return count


def test_decision_site_count_is_pinned():
    actual = _decision_site_count()
    assert actual == EXPECTED_DECISION_SITES, (
        f"guard.py decision-construction sites changed ({actual} != {EXPECTED_DECISION_SITES}). "
        "A new/removed decision path may need a DECISION_SOURCES change + a SCHEMA_VERSION bump. "
        "Review, then update EXPECTED_DECISION_SITES."
    )


def test_decision_sources_are_snake_case_and_nonempty():
    assert DECISION_SOURCES
    for source in DECISION_SOURCES:
        assert isinstance(source, str) and source
        assert source == source.lower()
        assert " " not in source and "-" not in source


def test_guard_has_no_monitor_decision_literal():
    text = GUARD.read_text(encoding="utf-8")
    assert '"monitor"' not in text and "'monitor'" not in text
