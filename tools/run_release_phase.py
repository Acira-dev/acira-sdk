"""Record a complete pytest phase in the process that actually runs its tests.

Use a python -c/runpy.run_path bootstrap to preserve normal cwd imports. This
tool never imports Acira to manufacture evidence or repairs the import path.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import sys
import sysconfig
import zipfile

PACKAGES = ("acira", "acira_cdp_proxy", "acira_dashboard")
SCHEMA = "acira-release-pytest-phase-v1"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def source_files(root, names):
    files = {}
    for name in names:
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or ".." in relative.parts
                or name != relative.as_posix() or name in files):
            raise ValueError("Invalid source manifest path")
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("Source manifest requires regular files")
        files[name] = digest(path)
    if not files:
        raise ValueError("Empty source manifest")
    return files


def inventory():
    entries = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name or not distribution.version:
            raise ValueError("Incomplete distribution inventory")
        entries.append({"name": name, "version": distribution.version})
    return sorted(entries, key=lambda item: (item["name"].lower(), item["version"]))


def prepare(root, directory, wheel, manifest=None, job_name=None):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if manifest:
        expected = json.loads(manifest.read_text())
        files = source_files(root, expected)
        if files != expected:
            raise ValueError("Provided source manifest differs")
    else:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                                capture_output=True, timeout=30)
        files = source_files(root, result.stdout.decode().rstrip("\0").split("\0"))
    identity = {"source_sha": os.environ.get("GITHUB_SHA"),
                "run_id": int(os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else None,
                "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]) if os.environ.get("GITHUB_RUN_ATTEMPT") else None,
                "job_id": None, "job_name": job_name, "wheel_sha256": digest(wheel) if wheel else None}
    save(directory / "source-manifest.json", files)
    save(directory / "identity.json", identity)
    save(directory / "inventory.json", inventory())
    for phase in ("source", "installed-wheel"):
        for name in ("home", "temp"):
            (directory / phase / name).mkdir(mode=0o700, parents=True)


def wheel_packages(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate wheel member")
        records = [name for name in names if name.endswith(".dist-info/RECORD")]
        if len(records) != 1:
            raise ValueError("Wheel RECORD missing or duplicated")
        entries = list(csv.reader(io.StringIO(archive.read(records[0]).decode())))
        if any(len(row) != 3 for row in entries) or len({row[0] for row in entries}) != len(entries):
            raise ValueError("Wheel RECORD rows invalid or duplicated")
        rows = {row[0]: row[1:] for row in entries}
        if set(rows) != {name for name in names if not name.endswith("/")} or rows[records[0]] != ["", ""]:
            raise ValueError("Wheel RECORD inventory differs")
        packages = {}
        for name in names:
            if name.endswith("/"):
                continue
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or name != relative.as_posix():
                raise ValueError("Invalid wheel package path")
            data = archive.read(name)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            if name != records[0] and rows.get(name) != ["sha256=" + encoded, str(len(data))]:
                raise ValueError("Wheel RECORD mismatch")
            if name.startswith(tuple(package + "/" for package in PACKAGES)):
                packages[name] = hashlib.sha256(data).hexdigest()
        if any(package + "/__init__.py" not in packages for package in PACKAGES):
            raise ValueError("Wheel package inventory incomplete")
        return packages


def origins():
    result = {}
    for name in PACKAGES:
        module = sys.modules.get(name)
        location = getattr(module, "__file__", None) if module is not None else None
        result[name] = str(Path(location).resolve()) if isinstance(location, str) else None
    return result


def package_files(root):
    result = {}
    for package in PACKAGES:
        directory = root / package
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("Package directory missing or linked")
        for path in directory.rglob("*"):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ValueError("Linked installed package file")
            if path.is_file():
                result[path.relative_to(root).as_posix()] = digest(path)
    return result


class Witness:
    def __init__(self, args):
        self.args = args
        self.nodeids = []
        self.observed = {}
        self.errors = []
        self.outcomes = dict(passed=0, failed=0, errors=0, skipped=0, deselected=0, xfailed=0, xpassed=0)

    def pytest_configure(self, config):
        if (list(config.invocation_params.args) != self.args or config.getini("addopts")
                or config.option.keyword or config.option.markexpr or config.option.collectonly):
            self.errors.append("pytest-options-differ")

    def pytest_collection_finish(self, session):
        self.nodeids = [item.nodeid for item in session.items]
        self.observed["collection"] = origins()

    def pytest_deselected(self, items):
        self.outcomes["deselected"] += len(items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.outcomes["errors"] += 1
        elif report.skipped:
            self.outcomes["skipped"] += 1

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            if report.skipped:
                key = "xfailed" if hasattr(report, "wasxfail") else "skipped"
            elif report.failed:
                key = "failed"
            elif report.passed:
                key = "xpassed" if hasattr(report, "wasxfail") else "passed"
            else:
                self.errors.append("unknown-test-report")
                return
            self.outcomes[key] += 1
        elif report.failed:
            self.outcomes["errors"] += 1
        elif report.skipped:
            self.outcomes["skipped"] += 1

    def pytest_sessionfinish(self, session, exitstatus):
        self.observed["sessionfinish"] = origins()


def run(root, inputs, phase, wheel, output, pytest_args):
    installed = phase == "installed-wheel"
    if (phase not in ("source", "installed-wheel") or len(pytest_args) != (4 if installed else 2)
            or pytest_args[0] != "-q" or not pytest_args[-1].startswith("--basetemp=")
            or Path(pytest_args[-1].split("=", 1)[1]).resolve() != output.parent / "pytest-temp"
            or installed and (Path(pytest_args[1]).resolve() != root / "tests"
                              or pytest_args[2] != "--import-mode=importlib")):
        raise ValueError("Only the complete release pytest command is accepted")
    if (os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") or os.environ.get("PYTEST_ADDOPTS")
            or not sys.path or sys.path[0] not in ("", str(Path.cwd()))):
        raise ValueError("Normal pytest startup and autoload are required")
    if phase == "source" and Path.cwd() != root:
        raise ValueError("Source pytest must run in the source checkout")
    if phase == "installed-wheel" and (Path.cwd().is_relative_to(root)
            or os.environ.get("ACIRA_TEST_INSTALLED") != "1"):
        raise ValueError("Installed phase must run outside source with its existing import guard")
    sidecar = output.with_suffix(".nodeids.json")
    if output.exists() or sidecar.exists() or (output.parent / "pytest-temp").exists():
        raise ValueError("Phase outputs and basetemp must be fresh")
    manifest_path = inputs / "source-manifest.json"
    identity_path = inputs / "identity.json"
    identity = json.loads(identity_path.read_text())
    identity_sha = digest(identity_path)
    if (type(identity) is not dict or set(identity) != {"source_sha", "run_id", "run_attempt", "job_id", "job_name", "wheel_sha256"}
            or any(identity[key] is not None and (type(identity[key]) is not str or not re.fullmatch(pattern, identity[key]))
                   for key, pattern in (("source_sha", r"[0-9a-f]{40}"), ("wheel_sha256", r"[0-9a-f]{64}"),
                                        ("job_name", r"[A-Za-z0-9_.-]{1,128}")))
            or any(identity[key] is not None and (type(identity[key]) is not int or identity[key] <= 0)
                   for key in ("run_id", "run_attempt", "job_id"))):
        raise ValueError("Invalid phase identity")
    expected_source = json.loads(manifest_path.read_text())
    before = digest(manifest_path)
    if source_files(root, expected_source) != expected_source:
        raise ValueError("Frozen source bytes differ before pytest")
    expected_packages = wheel_packages(wheel) if phase == "installed-wheel" else {
        name: value for name, value in expected_source.items() if name.startswith(tuple(p + "/" for p in PACKAGES))}
    wheel_sha = digest(wheel) if wheel else None
    if wheel_sha != identity["wheel_sha256"]:
        raise ValueError("Selected wheel differs from phase identity")
    inventory_path = inputs / "inventory.json"
    expected_inventory = json.loads(inventory_path.read_text())
    current_inventory = inventory()
    if not current_inventory or current_inventory != expected_inventory:
        raise ValueError("Prepared environment inventory differs")
    inventory_before = hashlib.sha256(canonical(current_inventory)).hexdigest()
    inventory_file_sha = digest(inventory_path)
    witness = Witness(pytest_args)
    record = dict(schema=SCHEMA, phase=phase, identity=identity, complete=False,
                  actual=dict(executable=sys.executable, prefix=sys.prefix, version=list(sys.version_info[:3]),
                              implementation=platform.python_implementation(),
                              free_threaded=bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
                              platform=sys.platform, machine=platform.machine(), args=pytest_args,
                              argv=list(sys.orig_argv), entrypoint_args=list(sys.argv),
                              cwd=str(Path.cwd()), default_plugin_autoload=True),
                  verification_errors=[], identity_file_sha256=identity_sha,
                  source_manifest={"before_sha256": before},
                  inventory={"file_sha256": inventory_file_sha, "before_sha256": inventory_before})
    actual_status = None
    primary = None
    try:
        import pytest
        actual_status = int(pytest.main(pytest_args, plugins=[witness]))
    except BaseException as error:
        primary = error
        record["verification_errors"].append("pytest-main-raised:" + type(error).__name__)
    finally:
        record["origins"] = dict(witness.observed, finish={name: None for name in PACKAGES})
        record["outcomes"] = dict(witness.outcomes, exitstatus=actual_status)
        record["collection"] = dict(count=len(witness.nodeids), nodeids_sha256=hashlib.sha256(canonical(witness.nodeids)).hexdigest(),
                                    nodeids_file=sidecar.name)
        record["verification_errors"] += witness.errors
        try:
            record["origins"]["finish"] = origins()
            after = digest(manifest_path)
            record["source_manifest"]["after_sha256"] = after
            if (after != before or source_files(root, expected_source) != expected_source
                    or digest(identity_path) != identity_sha):
                raise ValueError("Frozen source bytes differ after pytest")
            final = record["origins"]["finish"]
            if any(final[name] is None for name in PACKAGES):
                raise ValueError("Final loaded package origin missing")
            package_root = Path(final["acira"]).parent.parent
            if any(Path(final[name]) != package_root / name / "__init__.py" for name in PACKAGES):
                raise ValueError("Packages imported from different roots")
            if (phase == "source" and package_root != root or phase == "installed-wheel"
                    and (package_root.is_relative_to(root) or "site-packages" not in package_root.parts)):
                raise ValueError("Imported package phase differs")
            actual_packages = package_files(package_root)
            if actual_packages != expected_packages:
                raise ValueError("Loaded package bytes differ")
            package_sha = hashlib.sha256(canonical(actual_packages)).hexdigest()
            record["package_manifest_sha256"] = package_sha
            record["wheel"] = dict(sha256=wheel_sha, record_verified=phase == "installed-wheel",
                                    package_manifest_sha256=package_sha)
            if wheel and digest(wheel) != wheel_sha:
                raise ValueError("Wheel changed during pytest")
            inventory_after = hashlib.sha256(canonical(inventory())).hexdigest()
            record["inventory"]["after_sha256"] = inventory_after
            if inventory_after != inventory_before or digest(inventory_path) != inventory_file_sha:
                raise ValueError("Environment inventory changed during pytest")
        except BaseException as error:
            record["verification_errors"].append("phase-provenance:" + type(error).__name__)
            if primary is None and not actual_status and isinstance(error, (TimeoutError, KeyboardInterrupt, SystemExit)):
                primary = error
        record["complete"] = (primary is None and actual_status == 0 and not record["verification_errors"]
            and len(witness.nodeids) > 0 and len(set(witness.nodeids)) == len(witness.nodeids)
            and witness.outcomes["passed"] == len(witness.nodeids)
            and all(value == 0 for key, value in witness.outcomes.items() if key != "passed"))
        try:
            save(sidecar, witness.nodeids)
            save(output, record)
        except BaseException:
            if primary is None and not actual_status:
                raise
    if primary is not None:
        raise primary
    return actual_status if actual_status else (0 if record["complete"] else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--source-root", type=Path, required=True)
    prep.add_argument("--directory", type=Path, required=True)
    prep.add_argument("--wheel", type=Path)
    prep.add_argument("--source-manifest", type=Path)
    prep.add_argument("--job-name")
    phase = sub.add_parser("run")
    phase.add_argument("--source-root", type=Path, required=True)
    phase.add_argument("--inputs", type=Path, required=True)
    phase.add_argument("--phase", choices=("source", "installed-wheel"), required=True)
    phase.add_argument("--wheel", type=Path)
    phase.add_argument("--output", type=Path, required=True)
    phase.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = args.source_root.resolve()
    if args.command == "prepare":
        prepare(root, args.directory.resolve(), args.wheel, args.source_manifest, args.job_name)
        return 0
    return run(root, args.inputs.resolve(), args.phase, args.wheel, args.output.resolve(),
               args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args)


if __name__ == "__main__":
    raise SystemExit(main())
