"""Refuse publication when GitHub did not apply the requested release controls."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess


def verify_environment(environment: dict, branches: dict, reviewer_id: int) -> None:
    if not isinstance(environment, dict) or not isinstance(branches, dict):
        raise ValueError("GitHub returned an invalid environment response")
    if reviewer_id <= 0:
        raise ValueError("A repository-owner reviewer identity is required")
    if environment.get("can_admins_bypass") is not False:
        raise ValueError("Release approval must not permit administrator bypass")
    if environment.get("deployment_branch_policy") != {
        "protected_branches": False, "custom_branch_policies": True,
    }:
        raise ValueError("The publishing environment must use explicit branch restrictions")
    rules = environment.get("protection_rules")
    if not isinstance(rules, list):
        raise ValueError("GitHub did not return release protection rules")
    reviews = [r for r in rules if isinstance(r, dict) and r.get("type") == "required_reviewers"]
    if len(reviews) != 1:
        raise ValueError("Required release reviewers are missing; this GitHub plan may not support them for a private repository")
    review = reviews[0]
    if review.get("prevent_self_review") is not False:
        raise ValueError("The selected owner must be able to review its own release requests")
    reviewers = review.get("reviewers")
    if not isinstance(reviewers, list) or len(reviewers) != 1:
        raise ValueError("Release approval must be assigned to the selected owner")
    reviewer = reviewers[0]
    if not isinstance(reviewer, dict) or reviewer.get("type") != "User" or not isinstance(reviewer.get("reviewer"), dict):
        raise ValueError("Release approval must be assigned to the selected user account")
    if reviewer["reviewer"].get("id") != reviewer_id:
        raise ValueError("The release reviewer does not match the selected owner")
    policies = branches.get("branch_policies")
    if branches.get("total_count") != 1 or not isinstance(policies, list) or len(policies) != 1:
        raise ValueError("Only the main branch may publish")
    if not isinstance(policies[0], dict) or policies[0].get("name") != "main" or policies[0].get("type") != "branch":
        raise ValueError("The publishing branch rule must name main exactly, without tags or wildcards")


def github_json(path: str) -> dict:
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        # Never echo credential-bearing environment variables or CLI diagnostics.
        raise ValueError("Unable to verify release controls through the GitHub API; publication is blocked")
    if len(result.stdout) > 1024 * 1024:
        raise ValueError("GitHub returned an oversized release-control response")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("GitHub returned an invalid release-control response") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["pypi", "testpypi"])
    args = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        parser.error("GITHUB_REPOSITORY must identify the release repository")
    if os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        parser.error("Publishing requires a manual release request from main")
    owner_id = os.environ.get("GITHUB_REPOSITORY_OWNER_ID", "")
    if not owner_id.isdecimal():
        parser.error("GITHUB_REPOSITORY_OWNER_ID must identify the selected owner")
    path = f"repos/{repository}/environments/{args.environment}"
    try:
        verify_environment(github_json(path), github_json(path + "/deployment-branch-policies?per_page=100"), int(owner_id))
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Release blocked: {error}\n")
    print("Verified main-only publishing, the selected release reviewer and disabled admin bypass")


if __name__ == "__main__":
    main()
