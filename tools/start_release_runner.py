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
    OWNER_ID, REPOSITORY, VERIFICATION_PATH, PUBLISH_PATH, SUPPORTED_PYTHON_VERSIONS, verify_owner_request,
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


def python_metadata(interpreter):
    code = "import json,platform,sys; print(json.dumps(dict(version=list(sys.version_info[:3]),releaselevel=sys.version_info.releaselevel,architecture=platform.machine(),platform=sys.platform,prefix=sys.prefix)))"
    result = subprocess.run([str(interpreter), "-I", "-c", code],
        capture_output=True, text=True, timeout=30)
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("Unable to verify the installed Python interpreter")
    return json.loads(result.stdout)


def validate_mac_python(data, minor, prefix=None):
    if not isinstance(data, dict):
        raise ValueError("Invalid installed Python metadata")
    version = data.get("version")
    if (not isinstance(version, list) or len(version) != 3
            or any(type(part) is not int or part < 0 for part in version)
            or version[:2] != [3, int(minor.split(".")[1])]
            or data.get("releaselevel") != "final"
            or data.get("platform") != "darwin"
            or not isinstance(data.get("architecture"), str)
            or data["architecture"].lower() != platform.machine().lower()):
        raise ValueError("The local Python version, platform, or architecture does not match")
    if prefix is not None:
        location = data.get("prefix")
        if (not isinstance(location, str) or not location or not Path(location).is_absolute()
                or Path(location).resolve() != prefix.resolve()):
            raise ValueError("The cached Python must be isolated inside the temporary runner")
    return ".".join(map(str, version))


def prepare_mac_python_cache(directory):
    # GitHub's macOS Python installer writes to /Users/runner. Reuse native
    # interpreters in isolated venvs instead of creating privileged host folders.
    # Cache labels come from the actual executable, never the requested version.
    if sys.platform != "darwin":
        return
    architecture = {"arm64": "arm64", "x86_64": "x64"}.get(platform.machine().lower())
    if architecture is None:
        raise ValueError("Unsupported Mac runner architecture")
    for minor in SUPPORTED_PYTHON_VERSIONS:
        interpreter = sys.executable if sys.version_info[:2] == tuple(map(int, minor.split("."))) else shutil.which("python" + minor)
        if interpreter is None:
            raise ValueError("Mac release runners require all supported Python versions installed; no system folders will be created")
        version = validate_mac_python(python_metadata(interpreter), minor)
        destination = directory / "Python" / version / architecture
        result = subprocess.run([str(interpreter), "-m", "venv", str(destination)],
            capture_output=True, timeout=120)
        if result.returncode:
            raise ValueError("Unable to create an isolated temporary Python environment")
        cached = python_metadata(destination / "bin" / "python")
        if validate_mac_python(cached, minor, destination) != version:
            raise ValueError("Cached Python differs from the verified native interpreter")
        destination.with_name(architecture + ".complete").write_text("Verified local native Python in an isolated venv\n")


def select_mac_python_cache(directory, minor):
    if sys.platform != "darwin" or minor not in SUPPORTED_PYTHON_VERSIONS:
        raise ValueError("Native cache selection requires a supported Mac Python version")
    architecture = {"arm64": "arm64", "x86_64": "x64"}.get(platform.machine().lower())
    if architecture is None:
        raise ValueError("Unsupported Mac runner architecture")
    matches = []
    for version_dir in (directory / "Python").glob(minor + ".*"):
        if not re.fullmatch(re.escape(minor) + r"\.[0-9]+", version_dir.name):
            continue
        prefix = version_dir / architecture
        marker = prefix.with_name(architecture + ".complete")
        executable = prefix / "bin" / "python"
        if prefix.is_symlink() or marker.is_symlink() or not marker.is_file() or not executable.is_file():
            raise ValueError("Incomplete or redirected temporary Python cache")
        version = validate_mac_python(python_metadata(executable), minor, prefix)
        if version != version_dir.name:
            raise ValueError("Python cache label differs from the actual executable")
        matches.append((tuple(map(int, version.split("."))), executable))
    if not matches:
        raise ValueError("No verified native Python cache; start this Mac job with the temporary runner helper")
    return max(matches)[1]


def activate_mac_python(minor):
    executable = select_mac_python_cache(Path(os.environ["RUNNER_TOOL_CACHE"]), minor)
    prefix = executable.parents[1]
    if any(character in str(prefix) for character in "\r\n"):
        raise ValueError("Invalid Python environment path")
    with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as output:
        output.write(str(executable.parent) + "\n")
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as output:
        output.write("pythonLocation=" + str(prefix) + "\n")
    print("Selected verified native Mac Python " + prefix.parent.name)


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
            prepare_mac_python_cache(runner / "tool-cache")
            child_env["ACIRA_RELEASE_PYTHON_HELPER"] = str(Path(__file__).resolve())
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
                code = process.wait(timeout=60 * 95)
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
    parser.add_argument("--jobs", type=int, default=1, choices=range(1, 11))
    parser.add_argument("--check-job", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--activate-python", choices=SUPPORTED_PYTHON_VERSIONS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run_id <= 0 or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("Use the exact release run ID and source commit")
    try:
        if args.activate_python:
            check_job(args)
            activate_mac_python(args.activate_python)
        else:
            check_job(args) if args.check_job else run_runner(args)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        parser.exit(1, f"Temporary runner stopped: {error}\n")


if __name__ == "__main__":
    main()
