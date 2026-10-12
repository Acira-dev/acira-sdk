"""Tested adapter versions; installation never changes a user's framework."""
from __future__ import annotations

from importlib import metadata
import re


TESTED_FRAMEWORKS = {
    "playwright": {"distribution": "playwright", "versions": ("1.60.0", "1.61.0", "1.62.0", "1.63.0")},
    "browser_use": {"distribution": "browser-use", "versions": ("0.11.12", "0.11.13")},
}


def framework_compatibility(adapter: str) -> dict:
    spec = TESTED_FRAMEWORKS[adapter]
    package = spec["distribution"]
    try:
        installed = metadata.version(package)
    except metadata.PackageNotFoundError:
        installed = None
    status = "missing" if installed is None else "verified" if installed in spec["versions"] else "unverified"
    return {"distribution": package, "installed_version": installed,
        "tested_versions": list(spec["versions"]), "status": status,
        "verification_scope": "Adapter API coverage; this is not a dependency advisory audit",
        "boundary": "Only asynchronous Chromium integration and the documented supported operations",
        "installation": "Install the official framework separately in your project; Acira does not change its version",
        "guidance": "Use a tested framework version and run python -m pip check; see the setup guide before changing an existing environment"}


def require_verified_framework(adapter: str) -> None:
    status = framework_compatibility(adapter)
    if status["status"] != "verified":
        installed = status["installed_version"] or "not installed"
        tested = ", ".join(status["tested_versions"])
        raise ValueError(f"Acira cannot enable verified protection with {status['distribution']} {installed}. "
            f"Tested versions: {tested}. Install the official framework separately or use a compatible project environment; "
            "Acira will not downgrade or modify it automatically. Run acira doctor for compatibility details.")


# Offline evidence recorded on 2026-10-12, not a substitute for a current advisory audit.
_RECORDED_DEPENDENCY_FLOORS = {
    "anyio": {"known_fix_floor": "4.14.2", "release_minimum": "4.15.1", "major": 4,
              "sources": ["https://github.com/agronholm/anyio/security/advisories/GHSA-82r6-8w77-94w6",
                          "https://github.com/agronholm/anyio/security/advisories/GHSA-5p39-cfhj-2xmp"]},
    "pypdf": {"known_fix_floor": "6.19.0", "release_minimum": "6.20.0", "major": 6,
              "sources": ["https://pypi.org/pypi/pypdf/6.16.2/json"]},
}


def _stable_release(version: str) -> tuple[int, int, int] | None:
    # Base Acira has no packaging dependency. Unknown/vendor/prerelease versions
    # remain unverified rather than inventing a security comparison for them.
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:\.post[0-9]+)?", version)
    return tuple(map(int, match.groups())) if match else None


def dependency_checks(adapter: str) -> dict:
    evidence = {"advisory_audit": "not_performed", "recorded_on": "2026-10-12",
                "scope": "Offline recorded AnyIO/PyPDF fix floors only; not certification of the full environment",
                "packages": {}, "status": "not_applicable"}
    if adapter != "browser_use":
        return evidence
    for package, spec in _RECORDED_DEPENDENCY_FLOORS.items():
        try:
            installed = metadata.version(package)
        except metadata.PackageNotFoundError:
            installed = None
        release = _stable_release(installed) if installed is not None else None
        floor = _stable_release(spec["known_fix_floor"])
        minimum = _stable_release(spec["release_minimum"])
        if installed is None:
            status = "missing"
        elif release is None:
            status = "unverified_version"
        elif release < floor:
            status = "below_recorded_fix_floor"
        elif release[0] != spec["major"] or release < minimum:
            status = "outside_release_dependency_range"
        else:
            status = "meets_recorded_release_minimum"
        evidence["packages"][package] = {"installed_version": installed, "status": status,
            "known_fix_floor": spec["known_fix_floor"], "release_minimum": spec["release_minimum"],
            "sources": spec["sources"]}
    evidence["status"] = "meets_recorded_minima" if all(
        item["status"] == "meets_recorded_release_minimum" for item in evidence["packages"].values()
    ) else "needs_attention"
    evidence["guidance"] = (
        "Run python -m pip check. Review planned dependency changes with python -m pip install --dry-run \"acira[browser-use]\" "
        "before updating your existing project. No automatic installation or downgrade is performed. "
        "Use a fresh dependency advisory audit for the whole environment; these dated offline checks do not establish that it has no current vulnerabilities."
    )
    return evidence
