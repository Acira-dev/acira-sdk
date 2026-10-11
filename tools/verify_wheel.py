"""Release check: two clean environments and two folders share a late-started server.

Usage: python tools/verify_wheel.py /absolute/path/to/acira.whl
Uses a private temporary registry and never opens the user's browser.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv
from pathlib import Path


def main() -> None:
    wheel = Path(sys.argv[1]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="acira-wheel-") as temporary:
        root = Path(temporary)
        env = {**os.environ, "ACIRA_HOME": str(root / "private"), "ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false"}
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        interpreters = []
        agents = []
        def run(python, *args, cwd=None):
            result = subprocess.run([str(python), *args], env=env, cwd=cwd or root,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
            if result.returncode:
                raise RuntimeError(f"Wheel check command failed ({args[0]}): {result.stderr[-2000:]}")
            return result.stdout
        def descriptor():
            return json.loads((root / "private/server.json").read_text())
        def api(path):
            service = descriptor()
            request = urllib.request.Request(service["url"] + path, headers={"Authorization": "Bearer " + service["admin_key"]})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
                return json.load(response)
        def until(predicate, message, seconds=12):
            deadline = time.monotonic()+seconds
            while time.monotonic()<deadline:
                if predicate():
                    return
                time.sleep(.2)
            raise AssertionError(message)
        try:
            for name in ("orders", "support"):
                directory = root / name
                directory.mkdir()
                environment = root / (name + "-env")
                venv.EnvBuilder(with_pip=True).create(environment)
                python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                interpreters.append(python)
                run(python, "-m", "pip", "install", "--quiet", str(wheel))
                run(python, "-I", "-c", "from importlib.resources import files; import acira; assert 'site-packages' in acira.__file__; assets = files('acira_dashboard').joinpath('static'); required = ('workspace.html', 'workspace.css', 'workspace.js', 'theme.css', 'styles.css', 'index.html', 'google-sans.woff2', 'GeistMono-Regular.woff2', 'GoogleSans-OFL.txt', 'GeistMono-OFL.txt', 'acira-logo.png', 'acira-mark.png'); assert all(assets.joinpath(name).is_file() for name in required), 'Installed dashboard assets or font notices are missing'")
                assert "demo" in run(python, "-m", "acira", "--help")
                exported = root / (name + "-capture-extension")
                run(python, "-m", "acira", "extension", "install", "--directory", str(exported))
                manifest = json.loads((exported / "manifest.json").read_text())
                assert manifest["manifest_version"] == 3 and "debugger" not in manifest["permissions"]
                assert "host_permissions" not in manifest, "Production extension must retain explicit activeTab access"
                assert (exported / "content.js").is_file() and (exported / "assets/acira-logo.svg").is_file()
                assert (exported / "selection.html").is_file() and (exported / "selection.js").is_file(), "Capture selection assets are missing"
                run(python, "-m", "acira", "extension", "install", "--directory", str(exported))
                missing_extra = subprocess.run([str(python), "-m", "acira", "demo", "--no-open"],
                    env=env, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                assert missing_extra.returncode == 2 and "Playwright integration" in missing_extra.stderr, "Base wheel did not explain the optional Playwright dependency"
                run(python, "-m", "acira", "init", "--yes", "--policies", "none", cwd=directory)
                script = directory / "agent.py"
                script.write_text("""import asyncio
from pathlib import Path
from acira import protect
async def main():
    async with protect() as guard:
        while not Path('stop').exists():
            await guard.runtime_control.before_decision()
            await asyncio.sleep(.1)
asyncio.run(main())
""")
                agents.append(subprocess.Popen([str(python), str(script)], env=env, cwd=directory,
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            print("Clean wheel imports and file-only SDK startup: passed", flush=True)
            run(interpreters[0], "-m", "acira", "open", "--no-browser")
            first = descriptor()["instance"]
            run(interpreters[1], "-m", "acira", "open", "--no-browser")
            assert descriptor()["instance"] == first, "A second environment created a second service"
            def active():
                projects = api("/api/projects")["projects"]
                return len(projects) == 2 and all(p["runs"] and p["agents"][0]["state"] == "Active" and p["runs"][0]["connected"] for p in projects)
            until(active, "Agents did not connect to the late dashboard")
            projects = api("/api/projects")["projects"]
            support_before = next(p for p in projects if p["name"] == "support")["agents"][0]["revision"]
            run(interpreters[0], "-m", "acira", "policy", "add", "credentials", cwd=root / "orders")
            until(active, "Saved policy did not become Active")
            assert next(p for p in api("/api/projects")["projects"] if p["name"] == "support")["agents"][0]["revision"] == support_before
            print("Late connection, one service, separate project revisions: passed", flush=True)
            run(interpreters[1], "-m", "acira", "server", "stop")
            until(lambda: not (root / "private/server.json").exists(), "Server did not stop")
            assert all(agent.poll() is None for agent in agents), "Server stop terminated a local SDK"
            run(interpreters[1], "-m", "acira", "open", "--no-browser")
            until(active, "SDK did not reconnect after service restart")
            assert descriptor()["instance"] != first
            for project in api("/api/projects")["projects"]:
                records = api("/api/projects/"+project["id"]+"/events?limit=5000")["events"]
                ids = [record["event_id"] for record in records]
                assert len(ids) == len(set(ids)) and "[REDACTED]" not in ids
            print("Service restart, independent SDK lifetime, stable event IDs: passed", flush=True)
        finally:
            for name in ("orders", "support"):
                directory = root / name
                if directory.exists():
                    (directory / "stop").touch()
            for agent in agents:
                try:
                    agent.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    agent.terminate()
                    agent.wait(timeout=5)
            if interpreters and (root / "private/server.json").exists():
                run(interpreters[0], "-m", "acira", "server", "stop")
                until(lambda: not (root / "private/server.json").exists(), "Cleanup failed to stop the test server")


if __name__ == "__main__":
    main()
