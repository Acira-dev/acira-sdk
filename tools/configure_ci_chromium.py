"""Permit both installed Chromium executables to use their sandbox in CI.

This is disposable GitHub Actions setup, never SDK installation behavior.
Only literal executables from the installed Playwright CLI's dry-run metadata
receive AppArmor userns permissions. Kernel restrictions stay enabled.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


PROFILE_NAMES = {
    "chromium": "acira-ci-chromium",
    "chromium-headless-shell": "acira-ci-chromium-headless",
}


def literal_path(path: Path) -> str:
    value = path.as_posix()
    if (not path.is_absolute() or any(ord(character) < 32 or ord(character) == 127
                                     for character in value)
            or any(character in value for character in '\"\\*?[]{}')):
        raise ValueError("The CI Chromium executable must have a literal absolute AppArmor path")
    return value


def install_locations(output: str) -> dict[str, Path]:
    """Read only the two browser blocks in the official install dry run."""
    locations: dict[str, Path] = {}
    section = None
    for line in output.splitlines():
        if line and not line[0].isspace():
            match = re.search(r"\(playwright (chromium|chromium-headless-shell) v[0-9]+\)$", line)
            section = match.group(1) if match else None
        elif section and line.strip().startswith("Install location:"):
            if section in locations:
                raise ValueError("Ambiguous installed Chromium dry-run metadata")
            raw = line.strip().removeprefix("Install location:").strip()
            path = Path(raw)
            literal_path(path)
            locations[section] = path
    if set(locations) != set(PROFILE_NAMES):
        raise ValueError("Both installed Chromium browser locations are required")
    for name, path in locations.items():
        prefix = "chromium" if name == "chromium" else "chromium_headless_shell"
        if re.fullmatch(prefix + r"-[0-9]+", path.name) is None:
            raise ValueError("Unexpected installed Chromium browser location")
    if locations["chromium"].parent.resolve(strict=True) != locations["chromium-headless-shell"].parent.resolve(strict=True):
        raise ValueError("Installed Chromium browsers must share the declared browser cache")
    return locations


def installed_executables(output: str, chromium_executable: Path) -> dict[str, Path]:
    locations = install_locations(output)
    roots = {name: path.resolve(strict=True) for name, path in locations.items()}
    if any(root != locations[name].parent.resolve(strict=True) / locations[name].name
           for name, root in roots.items()):
        raise ValueError("Chromium browser directory escapes its declared install location")
    if any(not root.is_dir() for root in roots.values()):
        raise ValueError("Installed Chromium browser location is not a directory")
    chromium = chromium_executable.resolve(strict=True)
    literal_path(chromium)
    if not chromium.is_file() or not chromium.is_relative_to(roots["chromium"]):
        raise ValueError("Chromium executable escapes its declared install location")
    # The search stays inside one exact CLI-declared install directory. Refuse
    # multiple results and any symlink resolving outside that directory.
    candidates = list(roots["chromium-headless-shell"].rglob("chrome-headless-shell"))
    if len(candidates) != 1:
        raise ValueError("Exactly one installed Chromium headless executable is required")
    headless = candidates[0].resolve(strict=True)
    literal_path(headless)
    if not headless.is_file() or not headless.is_relative_to(roots["chromium-headless-shell"]):
        raise ValueError("Headless Chromium executable escapes its declared install location")
    if not os.access(chromium, os.X_OK) or not os.access(headless, os.X_OK):
        raise ValueError("Installed Chromium browser files must be executable")
    return {"chromium": chromium, "chromium-headless-shell": headless}


def profile_for(executable: Path, name: str = "acira-ci-chromium") -> str:
    if name not in PROFILE_NAMES.values():
        raise ValueError("Unknown task-owned Chromium AppArmor profile")
    path = literal_path(executable.resolve(strict=True))
    return ("abi <abi/4.0>,\ninclude <tunables/global>\n"
            f'profile {name} "{path}" flags=(unconfined) {{\n'
            "  userns,\n}\n")


async def main() -> None:
    if sys.platform != "linux":
        return
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This setup is restricted to disposable GitHub Actions runners")
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        executables = installed_executables(result.stdout, Path(playwright.chromium.executable_path))
        restriction = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
        restricted = restriction.exists() and restriction.read_text().strip() == "1"
        if restricted:
            with tempfile.TemporaryDirectory(prefix="acira-ci-apparmor-") as directory:
                for kind, executable in executables.items():
                    profile = Path(directory) / kind
                    profile.write_text(profile_for(executable, PROFILE_NAMES[kind]), encoding="utf-8")
                    subprocess.run(
                        ["sudo", "--non-interactive", "apparmor_parser", "--replace", str(profile)],
                        check=True, timeout=30,
                    )
            if restriction.read_text().strip() != "1":
                raise RuntimeError("The global user-namespace restriction must remain enabled")
            print("CI AppArmor userns permissions loaded for both exact Chromium executables")
        for kind in ("default headless", "explicit full"):
            options = {"headless": True, "chromium_sandbox": True, "timeout": 15000}
            if kind == "explicit full":
                options["executable_path"] = str(executables["chromium"])
            browser = await playwright.chromium.launch(**options)
            try:
                page = await browser.new_page()
                await page.goto("about:blank")
                assert await page.evaluate("1 + 1") == 2
                print(f"Sandbox-enabled {kind} Chromium startup verified")
            finally:
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
