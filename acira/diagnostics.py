"""Read-only setup diagnostics with explicit failures and repair steps."""
from __future__ import annotations

import importlib.metadata
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .project import Project


def command(*args: str) -> str:
    values = [sys.executable, *args]
    return subprocess.list2cmdline(values) if os.name == "nt" else shlex.join(values)


def chromium_check() -> dict:
    """Resolve Playwright's matching bundled build without launching a browser.

    A short subprocess also works when diagnostics are called from an existing
    asyncio application: Playwright's synchronous API cannot run in that loop.
    """
    probe = "from playwright.sync_api import sync_playwright\nimport json\nwith sync_playwright() as p:\n print(json.dumps({'path': p.chromium.executable_path}))"
    try:
        completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=10, check=True)
        path = Path(json.loads(completed.stdout)["path"])
        exists = path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))
        return {"status": "ready" if exists else "missing", "path": str(path),
                "scope": "Playwright's bundled Chromium; existing custom browser channels are not probed",
                "fix": command("-m", "playwright", "install", "chromium")}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {"status": "unavailable", "scope": "Matching bundled Chromium could not be resolved; no browser was launched",
                "fix": command("-m", "playwright", "install", "chromium")}


def doctor(project: Project, agent: str, *, skip_chromium: bool = False) -> dict:
    from .capabilities import capabilities
    from .framework_compatibility import dependency_checks, framework_compatibility
    from .service import verified_descriptor

    versions = {}
    for name in ("acira", "playwright", "browser-use"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    result = {"project": str(project.root), "agent": agent, "versions": versions,
              "python": {"version": sys.version.split()[0], "executable": sys.executable},
              "checks": {}, "failures": [], "warnings": [], "fixes": []}
    pending = (project.root / ".acira/policy-transaction.json").exists()
    result["checks"]["policy_transaction_pending"] = pending
    if pending:
        result["failures"].append("An interrupted policy write needs recovery")
        result["fixes"].append(f"acira doctor --project {shlex.quote(str(project.root))} --agent {shlex.quote(agent)} --recover")
    try:
        snapshot = project.snapshot(agent)
        adapter = snapshot.profile["adapter"]
        result.update(revision=snapshot.revision, policy_count=len(snapshot.documents))
        result["checks"]["configuration_valid"] = True
        folders = project.folders(agent)
        result["policy_folders"] = {key: str(path) for key, path in folders.items()}
        result["checks"].update(configuration_writable=os.access(project.path, os.W_OK),
                                policy_folders_writable=all(os.access(path, os.W_OK) for path in folders.values()),
                                free_disk_bytes=shutil.disk_usage(project.root).free)
        if not result["checks"]["configuration_writable"] or not result["checks"]["policy_folders_writable"]:
            result["warnings"].append("Project files are read-only: local enforcement can read them, but policy editing needs write permission")
        result["framework_compatibility"] = framework_compatibility(adapter)
        result["dependency_checks"] = dependency_checks(adapter)
        result["capabilities"] = capabilities(adapter)
        package = "browser-use" if adapter == "browser_use" else "playwright"
        result["checks"]["adapter_installed"] = versions[package] != "not installed"
        result["checks"]["framework_version_verified"] = result["framework_compatibility"]["status"] == "verified"
        if not result["checks"]["adapter_installed"]:
            tested = result["framework_compatibility"]["tested_versions"][-1]
            result["repair"] = f"Install the official framework separately: {command('-m', 'pip', 'install', package + '==' + tested)}; follow the setup guide for its supported dependency environment."
            result["failures"].append(f"{package} is not installed in this interpreter")
            result["fixes"].append(result["repair"])
        elif not result["checks"]["framework_version_verified"]:
            result["repair"] = "The installed framework version is unverified. Use a tested project environment; Acira will not change or downgrade it automatically."
            result["failures"].append(f"{package} {versions[package]} has not passed this release's protection tests")
            result["fixes"].append(result["repair"])
        if adapter == "browser_use":
            safe = result["dependency_checks"]["status"] == "meets_recorded_minima"
            result["checks"]["recorded_dependency_minima_satisfied"] = safe
            if not safe:
                result["dependency_repair"] = result["dependency_checks"]["guidance"]
                result["failures"].append("browser-use dependencies do not meet this release's recorded dependency requirements")
                result["fixes"].append(result["dependency_repair"])
        elif result["checks"]["adapter_installed"] and skip_chromium:
            result["browser"] = {"status": "skipped", "scope": "Custom browser channels/executables are not validated by doctor"}
            result["checks"]["bundled_chromium_available"] = None
            result["warnings"].append("Bundled Chromium check explicitly skipped. Verify your application's configured custom browser separately; this does not change enforcement.")
        elif result["checks"]["adapter_installed"]:
            result["browser"] = chromium_check()
            result["checks"]["bundled_chromium_available"] = result["browser"]["status"] == "ready"
            if not result["checks"]["bundled_chromium_available"]:
                result["failures"].append("Playwright's matching bundled Chromium is unavailable")
                result["fixes"].append(result["browser"]["fix"])
                result["warnings"].append("If your existing application uses an explicit custom browser channel or executable, this check does not validate it; use acira doctor --skip chromium to report that explicit diagnostic limitation")
    except (ValueError, OSError, TypeError) as exc:
        result["checks"]["configuration_valid"] = False
        result["failures"].append(str(exc))
        result["fixes"].append("Repair the named project/policy file, then run acira doctor again")
    try:
        found = verified_descriptor()
        result["dashboard"] = found["url"] if found else "not running"
        result["checks"]["dashboard_descriptor_valid"] = True
        if not found:
            result["warnings"].append("Optional dashboard is not running; file-based enforcement still works")
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        result["dashboard"] = "needs repair"
        result["checks"]["dashboard_descriptor_valid"] = False
        # Never include descriptor contents: it contains local credentials.
        result["failures"].append("Local dashboard discovery needs repair: the saved descriptor or service handshake is invalid")
        result["fixes"].append("Repair the local server descriptor's ownership/permissions or stop its original Acira process before reopening; do not trust an arbitrary process on its port")
    result["healthy"] = not result["failures"]
    result["integration"] = "Protection begins when protect() wraps your workflow and the supported adapter is attached. Diagnostics do not prove browser actions are guarded."
    return result


def print_doctor(result: dict) -> None:
    print(f"Acira doctor · {result['project']} / {result['agent']}")
    print(f"Python {result['python']['version']} · {result['python']['executable']}")
    print(" · ".join(f"{name} {value}" for name, value in result["versions"].items()))
    framework = result.get("framework_compatibility")
    if framework:
        print("Tested framework versions: " + ", ".join(framework["tested_versions"]))
    for failure in result["failures"]:
        print("FAIL: " + failure)
    for warning in result["warnings"]:
        print("NOTE: " + warning)
    for fix in dict.fromkeys(result["fixes"]):
        print("Fix: " + fix)
    print("Ready for integration." if result["healthy"] else "Setup needs attention; no browser was launched or policy changed.")
    print("Details: acira doctor --json")
