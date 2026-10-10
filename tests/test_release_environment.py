"""Missing or weaker provider controls must never authorize a package upload."""
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location("release_environment", Path(__file__).resolve().parents[1] / "tools/verify_release_environment.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
OWNER = 286463772
ENVIRONMENT = {
    "can_admins_bypass": False,
    "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    "protection_rules": [{"type": "required_reviewers", "prevent_self_review": False,
        "reviewers": [{"type": "User", "reviewer": {"id": OWNER}}]}],
}
BRANCHES = {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]}


def test_complete_approved_release_controls_are_accepted():
    release.verify_environment(deepcopy(ENVIRONMENT), deepcopy(BRANCHES), OWNER)


@pytest.mark.parametrize("case", ["missing-review", "bypass", "wrong-owner", "team", "self-review-blocked", "extra-reviewer", "all-branches", "wildcard", "tag", "extra-branch", "incomplete-page", "malformed-review"])
def test_missing_or_weaker_controls_block_upload(case):
    environment, branches = deepcopy(ENVIRONMENT), deepcopy(BRANCHES)
    review = environment["protection_rules"][0]
    if case == "missing-review":
        environment["protection_rules"] = [{"type": "branch_policy"}]
    elif case == "bypass":
        environment["can_admins_bypass"] = True
    elif case == "wrong-owner":
        review["reviewers"][0]["reviewer"]["id"] += 1
    elif case == "team":
        review["reviewers"][0]["type"] = "Team"
    elif case == "self-review-blocked":
        review["prevent_self_review"] = True
    elif case == "extra-reviewer":
        review["reviewers"].append({"type": "User", "reviewer": {"id": 1}})
    elif case == "all-branches":
        environment["deployment_branch_policy"] = None
    elif case == "wildcard":
        branches["branch_policies"][0]["name"] = "main*"
    elif case == "tag":
        branches["branch_policies"][0]["type"] = "tag"
    elif case == "extra-branch":
        branches["branch_policies"].append({"name": "release", "type": "branch"})
        branches["total_count"] = 2
    elif case == "incomplete-page":
        branches["total_count"] = 2
    elif case == "malformed-review":
        review["reviewers"] = [None]
    with pytest.raises(ValueError):
        release.verify_environment(environment, branches, OWNER)


@pytest.mark.parametrize("response", [SimpleNamespace(returncode=1, stdout="", stderr="private diagnostic"), SimpleNamespace(returncode=0, stdout="not json", stderr="")])
def test_api_failure_does_not_allow_publication_or_echo_private_diagnostics(monkeypatch, response):
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: response)
    with pytest.raises(ValueError) as failure:
        release.github_json("repos/Acira-dev/acira-sdk/environments/pypi")
    assert "private diagnostic" not in str(failure.value)
