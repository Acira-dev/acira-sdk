"""Publication remains fail closed under missing, forged or incomplete evidence."""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("release_environment", Path(__file__).resolve().parents[1] / "tools/verify_release_environment.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
SHA = "a" * 40
OWNER = {"id": 286463772, "login": "Acira-dev", "type": "User"}
REPOSITORY = {"full_name": "Acira-dev/acira-sdk", "private": True, "owner": OWNER}
ENVIRONMENT = {
    "can_admins_bypass": False,
    "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    "protection_rules": [{"type": "branch_policy"}],
}
BRANCHES = {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]}


def run_record(path=release.VERIFICATION_PATH, run_id=10):
    return {"id": run_id, "run_attempt": 1, "path": path, "head_sha": SHA,
        "event": "workflow_dispatch", "head_branch": "main", "actor": deepcopy(OWNER),
        "triggering_actor": deepcopy(OWNER), "repository": deepcopy(REPOSITORY),
        "head_repository": deepcopy(REPOSITORY), "status": "completed", "conclusion": "success",
        "created_at": "2026-10-11T00:00:00Z", "updated_at": "2026-10-11T01:00:00Z"}


def job_records():
    jobs = []
    for name, steps in release.REQUIRED_JOBS.items():
        jobs.append({"name": name, "head_sha": SHA, "run_id": 10, "run_attempt": 1,
            "status": "completed", "conclusion": "success",
            "started_at": "2026-10-11T00:00:00Z", "completed_at": "2026-10-11T01:00:00Z",
            "steps": [{"name": step, "status": "completed", "conclusion": "success",
                "started_at": "2026-10-11T00:00:00Z", "completed_at": "2026-10-11T01:00:00Z"} for step in steps]})
    return {"total_count": len(jobs), "jobs": jobs}


def artifact_record():
    return {"total_count": 1, "artifacts": [{"id": 50, "name": "python-distributions",
        "expired": False, "size_in_bytes": 100,
        "workflow_run": {"id": 10, "head_sha": SHA}}]}


def test_approved_free_controls_accept_owner_only_and_main_only():
    release.verify_owner_request(REPOSITORY, run_record(), SHA, release.VERIFICATION_PATH)
    release.verify_environment(ENVIRONMENT, BRANCHES)
    release.verify_completed_run(run_record(), job_records(), SHA)
    assert release.verify_artifact_record(artifact_record(), run_record()) == 50


@pytest.mark.parametrize("case", [
    "public", "different-repo", "wrong-owner", "organization", "collaborator",
    "collaborator-rerun", "different-sha", "pull-request", "tag", "other-workflow",
    "fork", "invalid-sha", "missing-trigger",
])
def test_unapproved_identity_or_source_cannot_publish(case):
    repo, request, sha = deepcopy(REPOSITORY), run_record(), SHA
    if case == "public": repo["private"] = False
    elif case == "different-repo": repo["full_name"] = "Other/acira-sdk"
    elif case == "wrong-owner": repo["owner"]["id"] += 1
    elif case == "organization": repo["owner"]["type"] = "Organization"
    elif case == "collaborator": request["actor"]["id"] += 1
    elif case == "collaborator-rerun": request["triggering_actor"]["id"] += 1
    elif case == "different-sha": request["head_sha"] = "b" * 40
    elif case == "pull-request": request["event"] = "pull_request"
    elif case == "tag": request["head_branch"] = "v0.1.0"
    elif case == "other-workflow": request["path"] = release.PUBLISH_PATH
    elif case == "fork": request["head_repository"]["full_name"] = "Other/acira-sdk"
    elif case == "invalid-sha": sha = "main"
    elif case == "missing-trigger": request.pop("triggering_actor")
    with pytest.raises(ValueError):
        release.verify_owner_request(repo, request, sha, release.VERIFICATION_PATH)


@pytest.mark.parametrize("case", ["bypass", "all-branches", "wildcard", "tag", "extra-branch", "incomplete-page", "missing-rules"])
def test_weaker_environment_still_blocks_upload(case):
    environment, branches = deepcopy(ENVIRONMENT), deepcopy(BRANCHES)
    if case == "bypass": environment["can_admins_bypass"] = True
    elif case == "all-branches": environment["deployment_branch_policy"] = None
    elif case == "wildcard": branches["branch_policies"][0]["name"] = "main*"
    elif case == "tag": branches["branch_policies"][0]["type"] = "tag"
    elif case == "extra-branch":
        branches["branch_policies"].append({"name": "release", "type": "branch"})
        branches["total_count"] = 2
    elif case == "incomplete-page": branches["total_count"] = 2
    elif case == "missing-rules": environment.pop("protection_rules")
    with pytest.raises(ValueError):
        release.verify_environment(environment, branches)


@pytest.mark.parametrize("case", [
    "failed-run", "in-progress", "missing-windows", "missing-linux", "duplicate-job",
    "unexecuted-job", "wrong-source", "old-attempt", "incomplete-page",
    "missing-audit-step", "skipped-step", "failed-step", "missing-step-times",
])
def test_all_actual_test_and_audit_checks_are_required(case):
    run, jobs = run_record(), job_records()
    if case == "failed-run": run["conclusion"] = "failure"
    elif case == "in-progress": run["status"] = "in_progress"
    elif case in ("missing-windows", "missing-linux"):
        marker = "Windows" if case == "missing-windows" else "Linux"
        jobs["jobs"] = [job for job in jobs["jobs"] if marker not in job["name"]]
        jobs["total_count"] = len(jobs["jobs"])
    elif case == "duplicate-job":
        jobs["jobs"].append(deepcopy(jobs["jobs"][0]))
        jobs["total_count"] += 1
    elif case == "unexecuted-job": jobs["jobs"][0]["started_at"] = None
    elif case == "wrong-source": jobs["jobs"][0]["head_sha"] = "b" * 40
    elif case == "old-attempt": jobs["jobs"][0]["run_attempt"] = 2
    elif case == "incomplete-page": jobs["total_count"] += 1
    else:
        audit = next(job for job in jobs["jobs"] if job["name"] == "dependency-audit")
        step = next(step for step in audit["steps"] if step["name"] == "Require no known dependency advisories")
        if case == "missing-audit-step": audit["steps"].remove(step)
        elif case == "skipped-step": step["conclusion"] = "skipped"
        elif case == "failed-step": step["conclusion"] = "failure"
        elif case == "missing-step-times": step.pop("started_at")
    with pytest.raises(ValueError):
        release.verify_completed_run(run, jobs, SHA)


@pytest.mark.parametrize("case", ["expired", "missing", "duplicate", "different-run", "different-sha", "oversize", "incomplete-page"])
def test_invalid_artifact_record_blocks_download(case):
    response = artifact_record()
    artifact = response["artifacts"][0]
    if case == "expired": artifact["expired"] = True
    elif case == "missing":
        response["artifacts"] = []
        response["total_count"] = 0
    elif case == "duplicate":
        response["artifacts"].append(deepcopy(artifact))
        response["total_count"] = 2
    elif case == "different-run": artifact["workflow_run"]["id"] = 11
    elif case == "different-sha": artifact["workflow_run"]["head_sha"] = "b" * 40
    elif case == "oversize": artifact["size_in_bytes"] = 26 * 1024 * 1024
    elif case == "incomplete-page": response["total_count"] += 1
    with pytest.raises(ValueError):
        release.verify_artifact_record(response, run_record())


def files(tmp_path):
    wheel, sdist = tmp_path / "acira-0.1.0-py3-none-any.whl", tmp_path / "acira-0.1.0.tar.gz"
    wheel.write_bytes(b"tested wheel")
    sdist.write_bytes(b"tested archive")
    return wheel, sdist, hashlib.sha256(wheel.read_bytes()).hexdigest(), hashlib.sha256(sdist.read_bytes()).hexdigest()


def test_exact_approved_files_are_accepted(tmp_path):
    wheel, sdist, wsha, ssha = files(tmp_path)
    assert release.verify_artifact_files(tmp_path, wsha, ssha) == [wheel, sdist]


@pytest.mark.parametrize("case", ["changed-wheel", "changed-source", "extra-file", "missing-source", "wrong-version", "symlink", "invalid-hash"])
def test_files_cannot_change_after_approval(tmp_path, case):
    wheel, sdist, wsha, ssha = files(tmp_path)
    if case == "changed-wheel": wheel.write_bytes(b"new build")
    elif case == "changed-source": sdist.write_bytes(b"new build")
    elif case == "extra-file": (tmp_path / "other.whl").write_bytes(b"unexpected")
    elif case == "missing-source": sdist.unlink()
    elif case == "wrong-version": sdist.rename(tmp_path / "acira-0.2.0.tar.gz")
    elif case == "symlink":
        contents = wheel.read_bytes()
        wheel.unlink()
        target = tmp_path.parent / (tmp_path.name + "-wheel")
        target.write_bytes(contents)
        wheel.symlink_to(target)
    elif case == "invalid-hash": wsha = "not a hash"
    with pytest.raises(ValueError):
        release.verify_artifact_files(tmp_path, wsha, ssha)


def mock_approval(monkeypatch):
    publishing = run_record(release.PUBLISH_PATH, 20)
    publishing["created_at"] = "2026-10-11T02:00:00Z"
    responses = {
        "repos/Acira-dev/acira-sdk": deepcopy(REPOSITORY),
        "repos/Acira-dev/acira-sdk/actions/runs/20": publishing,
        "repos/Acira-dev/acira-sdk/actions/runs/10": run_record(),
        "repos/Acira-dev/acira-sdk/actions/runs/10/jobs?filter=latest&per_page=100": job_records(),
        "repos/Acira-dev/acira-sdk/actions/runs/10/artifacts?per_page=100": artifact_record(),
        "repos/Acira-dev/acira-sdk/environments/pypi": deepcopy(ENVIRONMENT),
        "repos/Acira-dev/acira-sdk/environments/pypi/deployment-branch-policies?per_page=100": deepcopy(BRANCHES),
    }
    monkeypatch.setattr(release, "github_json", responses.__getitem__)
    for key, value in {"GITHUB_REPOSITORY": release.REPOSITORY, "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": SHA, "GITHUB_RUN_ID": "20"}.items():
        monkeypatch.setenv(key, value)
    args = SimpleNamespace(environment="pypi", verification_run=10, source_sha=SHA,
        wheel_sha256="c" * 64, sdist_sha256="d" * 64, artifact_directory=None)
    return args, responses


def test_complete_post_verification_owner_approval(monkeypatch, tmp_path):
    args, _ = mock_approval(monkeypatch)
    _, _, args.wheel_sha256, args.sdist_sha256 = files(tmp_path)
    args.artifact_directory = tmp_path
    assert release.verify_approval(args) == 50


@pytest.mark.parametrize("case", ["early-approval", "main-moved", "invalid-run", "invalid-hash", "owner-rerun"])
def test_publication_request_is_bound_to_post_verification_source(monkeypatch, case):
    args, responses = mock_approval(monkeypatch)
    if case == "early-approval":
        responses["repos/Acira-dev/acira-sdk/actions/runs/20"]["created_at"] = "2026-10-11T00:30:00Z"
    elif case == "main-moved": monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    elif case == "invalid-run": args.verification_run = 0
    elif case == "invalid-hash": args.wheel_sha256 = "bad"
    elif case == "owner-rerun": responses["repos/Acira-dev/acira-sdk/actions/runs/20"]["triggering_actor"]["id"] = 1
    with pytest.raises(ValueError):
        release.verify_approval(args)


@pytest.mark.parametrize("response", [
    SimpleNamespace(returncode=1, stdout="", stderr="private diagnostic"),
    SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    SimpleNamespace(returncode=0, stdout="[]", stderr=""),
])
def test_api_failure_does_not_allow_publication_or_echo_private_diagnostics(monkeypatch, response):
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: response)
    with pytest.raises(ValueError) as failure:
        release.github_json("repos/Acira-dev/acira-sdk/environments/pypi")
    assert "private diagnostic" not in str(failure.value)
