"""Permit the CI Chromium executable to use its sandbox on Ubuntu 24.04+.

This is an ephemeral GitHub-hosted-runner setup, never SDK installation behavior.
Only the exact Playwright Chromium path receives an AppArmor userns permission.
The kernel restriction and Chromium sandbox remain enabled. See Chromium's guide:
https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def profile_for(executable: Path) -> str:
    path = str(executable.resolve(strict=True))
    if any(character in path for character in '\n\r"\\*?[]{}'):
        raise ValueError("The CI Chromium executable must have a literal AppArmor path")
    return (
        "abi <abi/4.0>,\ninclude <tunables/global>\n"
        f'profile acira-ci-chromium "{path}" flags=(unconfined) {{\n'
        "  userns,\n}\n"
    )


async def main() -> None:
    if sys.platform != "linux":
        return
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This setup is restricted to disposable GitHub Actions runners")
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        restriction = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
        if restriction.exists() and restriction.read_text().strip() == "1":
            # Load directly from a temporary file: no broad wildcard profile, no
            # persistent /etc change and no system-wide sysctl/sandbox disabling.
            executable = Path(playwright.chromium.executable_path)
            with tempfile.TemporaryDirectory(prefix="acira-ci-apparmor-") as directory:
                profile = Path(directory) / "chromium"
                profile.write_text(profile_for(executable), encoding="utf-8")
                subprocess.run(
                    ["sudo", "--non-interactive", "apparmor_parser", "--replace", str(profile)],
                    check=True, timeout=30,
                )
            print("CI AppArmor userns permission loaded for the exact Chromium executable")
        browser = await playwright.chromium.launch(
            executable_path=playwright.chromium.executable_path,
            chromium_sandbox=True, timeout=15000,
        )
        try:
            page = await browser.new_page()
            await page.goto("about:blank")
            assert await page.evaluate("1 + 1") == 2
            print("Sandbox-enabled Chromium startup verified")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
