"""Require complete installed-package advisory evidence for the exact SDK wheel."""
from __future__ import annotations

import argparse
import email
from importlib import metadata
import json
from pathlib import Path
import re
import zipfile


def canonical_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", value):
        raise ValueError("Invalid package name in dependency evidence")
    return re.sub(r"[-_.]+", "-", value).lower()


def installed_inventory() -> list[dict[str, str]]:
    """Observe the same environment pip-audit inspected, without importing packages."""
    return sorted(
        ({"name": distribution.metadata.get("Name"), "version": distribution.version}
         for distribution in metadata.distributions()),
        key=lambda item: str(item["name"]).lower(),
    )


def inventory_versions(inventory: list[dict[str, str]]) -> dict[str, str]:
    if not isinstance(inventory, list) or not inventory:
        raise ValueError("Installed dependency inventory is missing")
    versions = {}
    for item in inventory:
        if not isinstance(item, dict):
            raise ValueError("Malformed installed dependency identity")
        name = canonical_name(item.get("name"))
        version = item.get("version")
        if not isinstance(version, str) or not version or name in versions:
            raise ValueError("Installed dependency inventory contains missing or duplicate identities")
        versions[name] = version
    return versions


def verify_report(report: dict, sdk_version: str, inventory: list[dict[str, str]]) -> int:
    expected_versions = inventory_versions(inventory)
    if expected_versions.get("acira") != sdk_version:
        raise ValueError("Installed SDK differs from the exact release wheel")
    dependencies = report.get("dependencies") if isinstance(report, dict) else None
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError("Dependency audit is missing its package evidence")
    seen = set()
    third_party = 0
    for item in dependencies:
        if not isinstance(item, dict):
            raise ValueError("Malformed dependency audit record")
        name = canonical_name(item.get("name"))
        if name in seen:
            raise ValueError("Dependency audit contains duplicate package names")
        seen.add(name)
        if name not in expected_versions:
            raise ValueError(f"Audit contains a package absent from the installed environment: {name}")
        if name == "acira" and "skip_reason" in item:
            expected = f"Dependency not found on PyPI and could not be audited: acira ({sdk_version})"
            if item["skip_reason"] != expected or item.get("vulns") or item.get("version", sdk_version) != sdk_version:
                raise ValueError("Unexpected missing Acira advisory evidence")
            continue
        if item.get("version") != expected_versions[name]:
            raise ValueError(f"Audit refers to a different installed version: {name}")
        if not isinstance(item.get("vulns"), list) or item["vulns"] or "skip_reason" in item:
            raise ValueError(f"Missing or unsafe dependency advisory evidence: {name}")
        if name != "acira":
            third_party += 1
    missing = expected_versions.keys() - seen
    if missing:
        raise ValueError("Dependency audit omitted installed packages: " + ", ".join(sorted(missing)))
    if not third_party:
        raise ValueError("Audit must cover the SDK and its third-party dependencies")
    return third_party


def wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        matches = [name for name in archive.namelist() if re.fullmatch(r"acira-[^/]+\.dist-info/METADATA", name)]
        if len(matches) != 1:
            raise ValueError("Expected one Acira wheel metadata record")
        wheel_metadata = email.message_from_bytes(archive.read(matches[0]))
        version = wheel_metadata.get("Version")
        if wheel_metadata.get("Name") != "acira" or not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError("Invalid Acira wheel identity")
        return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--inventory-output", type=Path, default=Path("dependency-audit-inventory.json"))
    parser.add_argument("--coverage-output", type=Path, default=Path("dependency-audit-coverage.json"))
    args = parser.parse_args()
    coverage = {"status": "rejected"}
    try:
        inventory = installed_inventory()
        args.inventory_output.write_text(json.dumps(inventory, indent=2) + "\n")
        version = wheel_version(args.wheel)
        count = verify_report(json.loads(args.report.read_text()), version, inventory)
        coverage = {"status": "verified", "sdk_version": version, "installed_package_count": len(inventory),
                    "audited_third_party_count": count, "inventory": str(args.inventory_output),
                    "report": str(args.report), "unpublished_sdk_exception": "Only the exact installed first-party wheel lookup may be unavailable"}
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        coverage["error"] = str(error)
        args.coverage_output.write_text(json.dumps(coverage, indent=2) + "\n")
        parser.exit(1, f"Dependency audit rejected: {error}\n")
    args.coverage_output.write_text(json.dumps(coverage, indent=2) + "\n")
    print(f"Verified complete advisory evidence for {count} third-party packages")


if __name__ == "__main__":
    main()
