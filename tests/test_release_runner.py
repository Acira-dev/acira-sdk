"""A temporary runner must reject jobs outside one selected owner-approved run."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("temporary_release_runner", TOOLS / "start_release_runner.py")
runner = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(TOOLS))
try:
    spec.loader.exec_module(runner)
finally:
    sys.path.remove(str(TOOLS))

SHA = "a" * 40
OWNER = {"id": 286463772, "login": "Acira-dev", "type": "User"}
REPO = {"full_name": "Acira-dev/acira-sdk", "private": True, "owner": OWNER}
RUN = {"id": 10, "head_sha": SHA, "status": "queued", "event": "workflow_dispatch",
    "head_branch": "main", "path": runner.VERIFICATION_PATH, "actor": OWNER,
    "triggering_actor": OWNER, "repository": REPO, "head_repository": REPO}


def test_selected_owner_verification_is_accepted():
    runner.verify_selected_run(REPO, RUN, 10, SHA)


@pytest.mark.parametrize("case", ["other-run", "ci-job", "pr", "other-commit", "collaborator", "finished"])
def test_other_jobs_are_rejected(case):
    run = deepcopy(RUN)
    if case == "other-run": run["id"] = 11
    elif case == "ci-job": run["path"] = ".github/workflows/ci.yml"
    elif case == "pr": run["event"] = "pull_request"
    elif case == "other-commit": run["head_sha"] = "b" * 40
    elif case == "collaborator": run["triggering_actor"]["id"] = 1
    elif case == "finished": run["status"] = "completed"
    with pytest.raises(ValueError):
        runner.verify_selected_run(REPO, run, 10, SHA)


@pytest.mark.parametrize("case", ["unsigned", "unofficial", "other-os", "duplicate"])
def test_download_requires_official_matching_checksummed_runner(monkeypatch, case):
    monkeypatch.setattr(runner.sys, "platform", "darwin")
    monkeypatch.setattr(runner.platform, "machine", lambda: "arm64")
    asset = {"os": "osx", "architecture": "arm64", "sha256_checksum": "c" * 64,
        "download_url": "https://github.com/actions/runner/releases/download/v2/runner.tar.gz"}
    assets = [asset]
    if case == "unsigned": asset.pop("sha256_checksum")
    elif case == "unofficial": asset["download_url"] = "https://example.com/runner"
    elif case == "other-os": asset["os"] = "win"
    elif case == "duplicate": assets.append(asset.copy())
    with pytest.raises(ValueError):
        runner.platform_asset(assets)


def test_pre_job_hook_rejects_before_api_access(monkeypatch):
    from types import SimpleNamespace
    args = SimpleNamespace(run_id=10, commit=SHA)
    monkeypatch.setenv("GITHUB_REPOSITORY", "other/repo")
    monkeypatch.setattr(runner, "api", lambda *args: pytest.fail("Unapproved job queried the release API"))
    with pytest.raises(ValueError):
        runner.check_job(args)
