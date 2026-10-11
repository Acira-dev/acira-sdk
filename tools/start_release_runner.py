"""Run bounded, one-job GitHub runners for one approved Acira release run.

Requires Python 3.12+, Git, and the GitHub CLI signed in as Acira-dev.
No service is installed. Registration is removed in finally, including interruption.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile

from verify_release_environment import (
    OWNER_ID, REPOSITORY, VERIFICATION_PATH, PUBLISH_PATH, verify_owner_request,
)


def api(path, method="GET"):
    result = subprocess.run(["gh", "api", "--hostname", "github.com", "--method", method, path],
        capture_output=True, text=True, timeout=30)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ValueError("GitHub runner setup failed; check Acira's GitHub CLI login and repository access")
    return json.loads(result.stdout) if result.stdout else None


def verify_selected_run(repository, run, run_id, commit):
    if run.get("id") != run_id or run.get("path") not in (VERIFICATION_PATH, PUBLISH_PATH):
        raise ValueError("Runner only accepts the selected Acira release workflow run")
    verify_owner_request(repository, run, commit, run["path"])
    if run.get("status") not in ("queued", "in_progress"):
        raise ValueError("The selected release run is not waiting for work")


def platform_asset(assets):
    system = {"darwin": "osx", "win32": "win", "linux": "linux"}.get(sys.platform)
    architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())
    matches = [asset for asset in assets if asset.get("os") == system and asset.get("architecture") == architecture]
    if system is None or architecture is None or len(matches) != 1:
        raise ValueError("No official runner is available for this operating system and architecture")
    asset = matches[0]
    if not re.fullmatch(r"[0-9a-f]{64}", asset.get("sha256_checksum", "")):
        raise ValueError("The official runner download has no valid checksum")
    if not asset.get("download_url", "").startswith("https://github.com/actions/runner/releases/download/"):
        raise ValueError("Runner download must come from the official GitHub runner release")
    return asset


def child_command(path, arguments=()):
    if sys.platform == "win32":
        return ["cmd.exe", "/c", str(path), *arguments]
    return [str(path), *arguments]


def create_hook(directory, run_id, commit):
    # Stored outside the runner/checkout; validate the job before any job step.
    helper = Path(__file__).resolve()
    args = ["--run-id", str(run_id), "--commit", commit, "--check-job"]
    if sys.platform == "win32":
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        hook = directory / "check-job.ps1"
        hook.write_text("& " + quote(sys.executable) + " " + quote(helper) + " " +
            " ".join(quote(value) for value in args) + "\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n", encoding="utf-8")
    else:
        import shlex
        hook = directory / "check-job.sh"
        hook.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(helper), *args]) + "\n")
        hook.chmod(0o700)
    return hook


def check_job(args):
    if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
            or os.environ.get("GITHUB_RUN_ID") != str(args.run_id)
            or os.environ.get("GITHUB_SHA") != args.commit
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("This job is outside the approved temporary runner scope")
    verify_selected_run(api("repos/" + REPOSITORY), api(f"repos/{REPOSITORY}/actions/runs/{args.run_id}"), args.run_id, args.commit)


def run_runner(args):
    if sys.version_info < (3, 12):
        raise ValueError("Use Python 3.12 or newer for temporary runner setup")
    if not shutil.which("gh") or not shutil.which("git"):
        raise ValueError("Install Git and GitHub CLI, then sign in as Acira-dev with gh auth login")
    if api("user").get("id") != OWNER_ID:
        raise ValueError("Sign into the GitHub CLI as Acira-dev")
    base = "repos/" + REPOSITORY
    verify_selected_run(api(base), api(f"{base}/actions/runs/{args.run_id}"), args.run_id, args.commit)
    asset = platform_asset(api(base + "/actions/runners/downloads"))
    with tempfile.TemporaryDirectory(prefix="acira-release-runner-") as location:
        private = Path(location)
        archive = private / asset["filename"]
        with urllib.request.urlopen(asset["download_url"], timeout=90) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != asset["sha256_checksum"]:
            raise ValueError("Official runner checksum mismatch")
        for _ in range(args.jobs):
            current = api(f"{base}/actions/runs/{args.run_id}")
            if current.get("status") == "completed":
                break
            verify_selected_run(api(base), current, args.run_id, args.commit)
            runner = private / ("job-" + uuid.uuid4().hex)
            runner.mkdir(mode=0o700)
            if archive.name.endswith(".zip"):
                with zipfile.ZipFile(archive) as source:
                    for entry in source.infolist():
                        relative = Path(entry.filename)
                        if relative.is_absolute() or ".." in relative.parts:
                            raise ValueError("Invalid official runner archive entry")
                    source.extractall(runner)
            else:
                with tarfile.open(archive) as source:
                    source.extractall(runner, filter="data")
            suffix = ".cmd" if sys.platform == "win32" else ".sh"
            name = "acira-release-" + uuid.uuid4().hex
            runner_id = None
            process = None
            child_env = os.environ.copy()
            child_env["ACTIONS_RUNNER_HOOK_JOB_STARTED"] = str(create_hook(private, args.run_id, args.commit))
            child_env["RUNNER_TOOL_CACHE"] = str(runner / "tool-cache")
            child_env["AGENT_TOOLSDIRECTORY"] = str(runner / "tool-cache")
            # Configuration output is captured so registration credentials cannot
            # appear in terminal output or a raised subprocess diagnostic.
            token = api(base + "/actions/runners/registration-token", "POST")["token"]
            try:
                try:
                    result = subprocess.run(child_command(runner / ("config" + suffix), [
                        "--unattended", "--url", "https://github.com/" + REPOSITORY,
                        "--token", token, "--name", name, "--ephemeral", "--disableupdate",
                        "--labels", f"acira-release-{args.run_id}", "--work", "_work",
                    ]), cwd=runner, env=child_env, capture_output=True, timeout=120)
                except (OSError, subprocess.SubprocessError):
                    raise ValueError("One-job runner registration could not complete") from None
                if result.returncode:
                    raise ValueError("One-job runner registration failed")
                records = api(base + "/actions/runners?per_page=100")
                selected = [item for item in records["runners"] if item.get("name") == name]
                if len(selected) != 1:
                    raise ValueError("Unable to confirm temporary runner identity")
                runner_id = selected[0]["id"]
                print(f"Temporary {platform.system()} runner is executing only release run {args.run_id}", flush=True)
                process = subprocess.Popen(child_command(runner / ("run" + suffix)), cwd=runner, env=child_env)
                # Bound the idle+job lifecycle, including interrupted setup.
                code = process.wait(timeout=60 * 55)
                if code != 0:
                    raise ValueError("Temporary runner stopped with an error; inspect this release run")
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=30)
                if runner_id is None:
                    records = api(base + "/actions/runners?per_page=100")
                    selected = [item for item in records["runners"] if item.get("name") == name]
                    runner_id = selected[0]["id"] if len(selected) == 1 else None
                if runner_id is not None:
                    remaining = api(base + "/actions/runners?per_page=100")
                    if any(item["id"] == runner_id for item in remaining["runners"]):
                        api(base + f"/actions/runners/{runner_id}", "DELETE")
                shutil.rmtree(runner, ignore_errors=False)
            # Check completion before registering another bounded one-job runner.
            time.sleep(3)
    print("Temporary runner registrations and files have been removed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--jobs", type=int, default=1, choices=range(1, 7))
    parser.add_argument("--check-job", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run_id <= 0 or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("Use the exact release run ID and source commit")
    try:
        check_job(args) if args.check_job else run_runner(args)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        parser.exit(1, f"Temporary runner stopped: {error}\n")


if __name__ == "__main__":
    main()
