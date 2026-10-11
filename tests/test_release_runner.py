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


def test_mac_cache_label_uses_verified_actual_version(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.platform, "machine", lambda: "arm64")
    data = {"version": [3, 12, 14], "releaselevel": "final", "platform": "darwin",
        "architecture": "arm64", "prefix": str(tmp_path)}
    assert runner.validate_mac_python(data, "3.12", tmp_path) == "3.12.14"


@pytest.mark.parametrize("case", ["minor", "malformed", "boolean", "prerelease", "platform", "architecture", "missing-prefix", "relative-prefix"])
def test_mac_cache_rejects_wrong_python_identity(monkeypatch, tmp_path, case):
    monkeypatch.setattr(runner.platform, "machine", lambda: "arm64")
    data = {"version": [3, 12, 14], "releaselevel": "final", "platform": "darwin",
        "architecture": "arm64", "prefix": str(tmp_path)}
    if case == "minor": data["version"] = [3, 11, 15]
    elif case == "malformed": data["version"] = [3, 12]
    elif case == "boolean": data["version"] = [3, 12, True]
    elif case == "prerelease": data["releaselevel"] = "alpha"
    elif case == "platform": data["platform"] = "linux"
    elif case == "architecture": data["architecture"] = "x86_64"
    elif case == "missing-prefix": data.pop("prefix")
    elif case == "relative-prefix": data["prefix"] = "relative/location"
    with pytest.raises(ValueError):
        runner.validate_mac_python(data, "3.12", tmp_path)


@pytest.mark.parametrize("native_minor", [(3, 11), (3, 12)])
def test_mac_cache_does_not_mark_a_different_cached_interpreter(monkeypatch, tmp_path, native_minor):
    from types import SimpleNamespace
    native_version = ".".join(map(str, (*native_minor, 15)))
    monkeypatch.setattr(runner, "sys", SimpleNamespace(
        platform="darwin", version_info=(*native_minor, 15),
        executable=f"/native/Python/{native_version}/arm64/bin/python",
    ))
    monkeypatch.setattr(runner.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/native/" + name)
    def metadata(executable):
        cached = Path(executable).is_relative_to(tmp_path)
        return {"version": [3, 11, 16 if cached else 15], "releaselevel": "final",
            "platform": "darwin", "architecture": "arm64",
            "prefix": str(Path(executable).parents[1]) if cached else "/native"}
    monkeypatch.setattr(runner, "python_metadata", metadata)
    def create_venv(args, **kwargs):
        Path(args[-1]).mkdir(parents=True)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, "run", create_venv)
    with pytest.raises(ValueError):
        runner.prepare_mac_python_cache(tmp_path)
    assert not list(tmp_path.rglob("*.complete"))


def test_non_mac_cache_setup_does_not_touch_interpreters(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(runner, "python_metadata", lambda *args: pytest.fail("Linux used Mac-specific cache setup"))
    runner.prepare_mac_python_cache(tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.fixture
def native_mac_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.sys, "platform", "darwin")
    monkeypatch.setattr(runner.platform, "machine", lambda: "arm64")
    prefix = tmp_path / "Python" / "3.12.14" / "arm64"
    (prefix / "bin").mkdir(parents=True)
    executable = prefix / "bin" / "python"
    executable.write_text("native interpreter fixture")
    marker = prefix.with_name("arm64.complete")
    marker.write_text("verified")
    data = {"version": [3, 12, 14], "releaselevel": "final", "platform": "darwin",
        "architecture": "arm64", "prefix": str(prefix)}
    monkeypatch.setattr(runner, "python_metadata", lambda executable: data)
    return tmp_path, prefix, executable, marker, data


def test_native_selection_returns_the_verified_minor(native_mac_cache):
    directory, _, executable, _, _ = native_mac_cache
    assert runner.select_mac_python_cache(directory, "3.12") == executable
    with pytest.raises(ValueError):
        runner.select_mac_python_cache(directory, "3.11")


@pytest.mark.parametrize("case", ["minor", "platform", "marker", "label", "prefix", "no-cache"])
def test_native_selection_rejects_incomplete_or_false_evidence(monkeypatch, native_mac_cache, case):
    directory, prefix, _, marker, data = native_mac_cache
    minor = "3.12"
    if case == "minor": minor = "3.10"
    elif case == "platform": monkeypatch.setattr(runner.sys, "platform", "linux")
    elif case == "marker": marker.unlink()
    elif case == "label": data["version"] = [3, 12, 15]
    elif case == "prefix": data["prefix"] = str(directory / "outside")
    elif case == "no-cache": directory = directory / "empty"
    with pytest.raises(ValueError):
        runner.select_mac_python_cache(directory, minor)
