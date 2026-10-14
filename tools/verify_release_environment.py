"""Acira-owner approval of a completed verification and an exact artifact pair.

The owner selected this free workflow approval instead of GitHub's paid private
repository reviewer feature. It is not an independent review service.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPOSITORY = "Acira-dev/acira-sdk"
OWNER_ID = 286463772
VERIFICATION_PATH = ".github/workflows/verify-release.yml"
PUBLISH_PATH = ".github/workflows/release.yml"
MATRIX_STEPS = {
    "Validate platform", "Install release and check dependencies", "Install Chromium",
    "Test source checkout", "Test installed distribution",
    "Verify clean installation journey", "Check dashboard and extension scripts",
}
SUPPORTED_PYTHON_VERSIONS = ("3.11", "3.12", "3.13", "3.14", "3.15")

REQUIRED_JOBS = {
    "authorize-verification": {"Check Acira owner"},
    "verification-summary": {"Show exact approval inputs"},
    "build": {"Build distributions", "Validate distributions", "Assemble release bundle", "Upload distributions"},
    "dependency-audit": {"Install audited release", "Check dependency consistency", "Require no known dependency advisories", "Validate audit coverage"},
    **{f"verify-{system}-py{version}": MATRIX_STEPS for system in ("Linux", "macOS", "Windows") for version in SUPPORTED_PYTHON_VERSIONS},
}
PLATFORM_SCOPES = {
    "all": ("Linux", "macOS", "Windows"),
    "macos-linux": ("Linux", "macOS"),
}


def required_jobs(platforms: str) -> dict:
    if platforms not in PLATFORM_SCOPES:
        raise ValueError("Unknown release platform scope")
    return {
        name: steps for name, steps in REQUIRED_JOBS.items()
        if not name.startswith("verify-")
        or any(name.startswith(f"verify-{system}-") for system in PLATFORM_SCOPES[platforms])
    }


def verify_owner_request(repository: dict, request: dict, source_sha: str, path: str) -> None:
    owner = repository.get("owner", {})
    if (repository.get("full_name") != REPOSITORY or repository.get("private") is not True
            or owner.get("id") != OWNER_ID or owner.get("login", "").lower() != "acira-dev"
            or owner.get("type") != "User"):
        raise ValueError("Release requires the private Acira-owned SDK repository")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Approval requires an exact source commit")
    if (request.get("event") != "workflow_dispatch" or request.get("head_branch") != "main"
            or request.get("head_sha") != source_sha or request.get("path") != path):
        raise ValueError("Release requires a manual request from the exact verified main commit and workflow")
    for field in ("actor", "triggering_actor"):
        actor = request.get(field, {})
        if actor.get("id") != OWNER_ID or actor.get("login", "").lower() != "acira-dev":
            raise ValueError("Only the Acira owner may request or rerun a release")
    for field in ("repository", "head_repository"):
        if request.get(field, {}).get("full_name") != REPOSITORY:
            raise ValueError("Release requests from other repositories are not accepted")


def verify_environment(environment: dict, branches: dict) -> None:
    if environment.get("can_admins_bypass") is not False:
        raise ValueError("Publishing must not permit administrator bypass")
    if environment.get("deployment_branch_policy") != {
        "protected_branches": False, "custom_branch_policies": True,
    }:
        raise ValueError("The publishing environment must use explicit branch restrictions")
    if not isinstance(environment.get("protection_rules"), list):
        raise ValueError("GitHub did not return publishing protection rules")
    policies = branches.get("branch_policies")
    if branches.get("total_count") != 1 or not isinstance(policies, list) or len(policies) != 1:
        raise ValueError("Only the main branch may publish")
    if not isinstance(policies[0], dict) or policies[0].get("name") != "main" or policies[0].get("type") != "branch":
        raise ValueError("The publishing branch rule must name main exactly, without tags or wildcards")


def verify_completed_run(run: dict, jobs: dict, source_sha: str, platforms: str = "all") -> None:
    required = required_jobs(platforms)
    # The trusted workflow records the owner's selected scope in its run name.
    # A partial all-platform run cannot be relabelled as a smaller release.
    if platforms != "all" and run.get("display_title") != f"Acira release verification ({platforms})":
        raise ValueError("Verification was not requested for the approved platform scope")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError("The verification run has not passed")
    records = jobs.get("jobs")
    if not isinstance(records, list) or jobs.get("total_count") != len(records):
        raise ValueError("Incomplete verification-job evidence")
    names = [job.get("name") for job in records]
    if len(set(names)) != len(names) or not required.keys() <= set(names):
        raise ValueError("Required verification jobs are missing or duplicated")
    for job in records:
        if (job.get("status") != "completed" or job.get("conclusion") != "success"
                or job.get("head_sha") != source_sha or job.get("run_id") != run.get("id")
                or job.get("run_attempt") != run.get("run_attempt") or not job.get("started_at")
                or not job.get("completed_at")):
            raise ValueError("A verification job did not execute and pass for this commit and attempt")
        for name in required.get(job.get("name"), set()):
            matches = [step for step in job.get("steps", []) if step.get("name") == name]
            if (len(matches) != 1 or matches[0].get("conclusion") != "success"
                    or matches[0].get("status") != "completed"
                    or not matches[0].get("started_at") or not matches[0].get("completed_at")):
                raise ValueError(f"Required verification step did not execute: {name}")


def verify_artifact_record(response: dict, run: dict) -> int:
    artifacts = response.get("artifacts")
    if not isinstance(artifacts, list) or response.get("total_count") != len(artifacts):
        raise ValueError("Incomplete release-artifact evidence")
    matches = [item for item in artifacts if item.get("name") == "python-distributions"]
    if len(matches) != 1:
        raise ValueError("One tested Python distribution artifact is required")
    artifact = matches[0]
    identity = artifact.get("workflow_run", {})
    if (artifact.get("expired") is not False or not isinstance(artifact.get("id"), int)
            or artifact["id"] <= 0 or not 0 < artifact.get("size_in_bytes", 0) <= 25 * 1024 * 1024
            or identity.get("id") != run.get("id") or identity.get("head_sha") != run.get("head_sha")):
        raise ValueError("Release artifacts are expired, unavailable, or from another verification run")
    return artifact["id"]


def verify_artifact_files(directory: Path, wheel_sha256: str, sdist_sha256: str) -> list[Path]:
    for expected in (wheel_sha256, sdist_sha256):
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Approval requires both exact SHA-256 hashes")
    files = list(directory.iterdir())
    wheels = [p for p in files if re.fullmatch(r"acira-[0-9]+\.[0-9]+\.[0-9]+-py3-none-any\.whl", p.name)]
    if len(files) != 2 or len(wheels) != 1:
        raise ValueError("The upload directory must contain only the approved wheel and source archive")
    wheel = wheels[0]
    sdist = directory / (wheel.name.removesuffix("-py3-none-any.whl") + ".tar.gz")
    for path, expected in ((wheel, wheel_sha256), (sdist, sdist_sha256)):
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 25 * 1024 * 1024:
            raise ValueError("Invalid distribution file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Distribution hash differs from the owner's approval")
    return [wheel, sdist]


def github_json(path: str) -> dict:
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise ValueError("Unable to verify release controls through the GitHub API; publication is blocked")
    if len(result.stdout) > 1024 * 1024:
        raise ValueError("GitHub returned an oversized release-control response")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("GitHub returned an invalid release-control response") from error
    if not isinstance(value, dict):
        raise ValueError("GitHub returned an invalid release-control response")
    return value


def verify_approval(args) -> int:
    if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_SHA") != args.source_sha):
        raise ValueError("Publish must use the exact verified main commit")
    publishing_id = os.environ.get("GITHUB_RUN_ID", "")
    if not publishing_id.isdecimal() or args.verification_run <= 0:
        raise ValueError("Both publishing and verification run identities are required")
    for digest in (args.wheel_sha256, args.sdist_sha256):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Approval requires exact distribution hashes")
    base = f"repos/{REPOSITORY}"
    repository = github_json(base)
    publishing = github_json(f"{base}/actions/runs/{publishing_id}")
    verification = github_json(f"{base}/actions/runs/{args.verification_run}")
    verify_owner_request(repository, publishing, args.source_sha, PUBLISH_PATH)
    verify_owner_request(repository, verification, args.source_sha, VERIFICATION_PATH)
    if datetime.fromisoformat(verification["updated_at"].replace("Z", "+00:00")) >= datetime.fromisoformat(publishing["created_at"].replace("Z", "+00:00")):
        raise ValueError("Owner approval must be requested after verification finishes")
    verify_completed_run(verification, github_json(f"{base}/actions/runs/{args.verification_run}/jobs?filter=latest&per_page=100"), args.source_sha, getattr(args, "platforms", "all"))
    environment = f"{base}/environments/{args.environment}"
    verify_environment(github_json(environment), github_json(environment + "/deployment-branch-policies?per_page=100"))
    artifact_id = verify_artifact_record(github_json(f"{base}/actions/runs/{args.verification_run}/artifacts?per_page=100"), verification)
    if args.artifact_directory:
        verify_artifact_files(args.artifact_directory, args.wheel_sha256, args.sdist_sha256)
    return artifact_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["pypi", "testpypi"])
    parser.add_argument("--platforms", choices=PLATFORM_SCOPES, default="all")
    parser.add_argument("--verification-run", required=True, type=int)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--sdist-sha256", required=True)
    parser.add_argument("--artifact-directory", type=Path)
    args = parser.parse_args()
    try:
        artifact_id = verify_approval(args)
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Release blocked: {error}\n")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"artifact_id={artifact_id}\n")
    print(f"Verified Acira owner approval, complete {args.platforms} test matrix, audit and main-only environment" +
          ("; exact artifact hashes match" if args.artifact_directory else "; artifact download still required"))


if __name__ == "__main__":
    main()
