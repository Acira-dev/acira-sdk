"""Loaded capture extension -> actual CLI -> terminal consent -> browser effects."""
from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect

from acira.extension import install_extension
from acira.journal import Journal
from acira.project import Project
from acira_dashboard.server import create_app, DashboardPaths


AGENT = r'''
import asyncio,json,sys
from acira import protect
from acira_cdp_proxy.action_gateway import AciraActionBlocked
from playwright.async_api import async_playwright
async def main():
    results=[]
    async with protect(project=sys.argv[1],dashboard=False) as guard,async_playwright() as p:
        guard.patch_playwright(p)
        browser=await p.chromium.launch(headless=True,chromium_sandbox=True,
            args=['--password-store=basic','--use-mock-keychain'])
        try:
            page=await browser.new_page()
            await page.goto(sys.argv[2])
            for attempt in range(2):
                try:
                    await page.locator('#second').click()
                    results.append('allowed')
                except AciraActionBlocked:
                    results.append('blocked')
                print('ATTEMPT_DONE='+str(attempt),flush=True)
            await page.goto(sys.argv[2])
            try:
                await page.locator('#third').click()
                results.append('unexpected_allow')
            except AciraActionBlocked:
                results.append('blocked')
            print('FLOW_RESULT='+json.dumps(results),flush=True)
        finally:
            await browser.close()
asyncio.run(main())
'''


def _env(tmp_path):
    env = {**os.environ, "ACIRA_HOME": str(tmp_path / "private"), "PYTHONUNBUFFERED": "1",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "PIP_KEYRING_PROVIDER": "disabled"}
    if os.environ.get("ACIRA_TEST_INSTALLED") != "1":
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return env


def _cleanup_terminal_flow(child, master, slave, transcript, tmp_path, primary):
    errors = []
    def attempt(stage, callback):
        try:
            return callback()
        except BaseException as error:
            errors.append((stage, error))
    # Only this direct Popen-owned child may be signalled. A failed signal must
    # not skip its bounded wait or the independent transcript/descriptor cleanup.
    if child is not None:
        running = attempt("poll", lambda: child.poll() is None)
        if running:
            attempt("terminate", child.terminate)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                attempt("kill", child.kill)
                attempt("kill_wait", lambda: child.wait(timeout=5))
            except BaseException as error:
                errors.append(("terminate_wait", error))
    attempt("transcript", lambda: (tmp_path / "terminal-transcript.txt").write_bytes(transcript))
    attempt("master_close", lambda: os.close(master))
    if slave is not None:
        attempt("slave_close", lambda: os.close(slave))
    attempt("cleanup_record", lambda: (tmp_path / "terminal-cleanup.json").write_text(json.dumps({
        "errors": [{"stage": stage, "error_type": type(error).__name__} for stage, error in errors]
    }, indent=2) + "\n"))
    if errors:
        if primary is None:
            raise errors[0][1]
        primary.add_note("Terminal cleanup errors: " + json.dumps([
            {"stage": stage, "error_type": type(error).__name__} for stage, error in errors]))


def _terminal_flow(project, url, tmp_path):
    import pty
    master, slave = pty.openpty()
    child = None
    transcript = bytearray()
    def until(predicate):
        deadline = time.monotonic() + 45
        while not predicate(transcript.decode(errors="replace")):
            if time.monotonic() >= deadline:
                raise AssertionError("Real terminal flow exceeded its stage deadline")
            readable, _, _ = select.select([master], [], [], .1)
            if readable:
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        raise AssertionError("Agent closed before the expected terminal stage") from None
                    raise
                if not data:
                    raise AssertionError("Agent closed before the expected terminal stage")
                transcript.extend(data)
    try:
        child = subprocess.Popen([sys.executable, "-B", "-c", AGENT, str(project), url],
            stdin=slave, stdout=slave, stderr=slave, env=_env(tmp_path))
        os.close(slave)
        slave = None
        until(lambda text: text.count("[y/N]") == 1)
        os.write(master, b"n\n")
        until(lambda text: "ATTEMPT_DONE=0" in text and text.count("[y/N]") == 2)
        os.write(master, b"y\n")
        until(lambda text: "FLOW_RESULT=" in text)
        assert child.wait(timeout=10) == 0
        lines = transcript.decode(errors="replace").splitlines()
        result = next(line.split("FLOW_RESULT=", 1)[1] for line in lines if "FLOW_RESULT=" in line)
        assert json.loads(result) == ["blocked", "allowed", "blocked"]
        assert sum("[y/N]" in line for line in lines) == 2
    finally:
        _cleanup_terminal_flow(child, master, slave, transcript, tmp_path, sys.exc_info()[1])


def test_terminal_cleanup_preserves_original_failure_and_attempts_all_owned_cleanup(tmp_path, monkeypatch):
    primary = AssertionError("original terminal stage failed")
    steps = []
    class Child:
        def poll(self):
            return None
        def terminate(self):
            steps.append("terminate")
            raise OSError("synthetic signal failure")
        def wait(self, timeout):
            steps.append(("wait", timeout))
            if timeout == 10:
                raise subprocess.TimeoutExpired("owned fixture", timeout)
            return 0
        def kill(self):
            steps.append("kill")
            raise RuntimeError("synthetic kill failure")
    def close(descriptor):
        steps.append(("close", descriptor))
        if descriptor == 100:
            raise OSError("synthetic descriptor failure")
    monkeypatch.setattr(os, "close", close)
    with pytest.raises(AssertionError) as observed:
        try:
            raise primary
        finally:
            _cleanup_terminal_flow(Child(), 100, 101, b"inert transcript", tmp_path, sys.exc_info()[1])
    assert observed.value is primary
    assert steps == ["terminate", ("wait", 10), "kill", ("wait", 5), ("close", 100), ("close", 101)]
    assert (tmp_path / "terminal-transcript.txt").read_bytes() == b"inert transcript"
    assert json.loads((tmp_path / "terminal-cleanup.json").read_text())["errors"] == [
        {"stage": "terminate", "error_type": "OSError"},
        {"stage": "kill", "error_type": "RuntimeError"},
        {"stage": "master_close", "error_type": "OSError"}]


def test_terminal_cleanup_without_prior_failure_raises_exact_cleanup_error(tmp_path, monkeypatch):
    error = OSError("synthetic descriptor failure")
    closed = []
    def close(descriptor):
        closed.append(descriptor)
        if descriptor == 100:
            raise error
    monkeypatch.setattr(os, "close", close)
    with pytest.raises(OSError) as observed:
        _cleanup_terminal_flow(None, 100, 101, b"inert transcript", tmp_path, None)
    assert observed.value is error and closed == [100, 101]
    assert (tmp_path / "terminal-transcript.txt").read_bytes() == b"inert transcript"
    assert json.loads((tmp_path / "terminal-cleanup.json").read_text())["errors"] == [
        {"stage": "master_close", "error_type": "OSError"}]


@pytest.mark.asyncio
async def test_loaded_capture_edit_cli_import_and_real_terminal_browser_flow(tmp_path, monkeypatch, select_capture_target):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    hits = []
    async def index(request):
        hits.append(request.path)
        if request.path == "/":
            return web.Response(text='''<link rel="icon" href="data:,"><title>Inert capture fixture</title>
                <main style="margin:50px"><a id="first" href="/first">Continue</a><br><br>
                <a id="second" href="/second">Continue</a><br><br>
                <a id="third" href="/third">Continue</a></main>''', content_type="text/html")
        return web.Response(text='<link rel="icon" href="data:,"><h1>Inert destination</h1>', content_type="text/html")
    app = web.Application()
    app.router.add_get('/{tail:.*}', index)
    project = tmp_path / "existing-project"
    env = _env(tmp_path)
    def cli(*args):
        completed = subprocess.run([sys.executable, "-B", "-m", "acira", *args, "--project", str(project)],
            env=env, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
        return completed.stdout
    await asyncio.to_thread(cli, "init", "--yes", "--adapter", "playwright", "--policies", "none", "--no-dashboard")
    extension = install_extension(tmp_path / "extension")
    manifest = json.loads((extension / "manifest.json").read_text())
    manifest["host_permissions"] = ["http://127.0.0.1/*"]
    (extension / "manifest.json").write_text(json.dumps(manifest))
    exported = tmp_path / "capture.json"
    async with TestServer(app, host="127.0.0.1") as website:
        url = str(website.make_url('/'))
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(tmp_path / "anonymous-profile", channel="chromium",
                headless=True, chromium_sandbox=True, args=["--password-store=basic", "--use-mock-keychain",
                    "--disable-extensions-except=" + str(extension), "--load-extension=" + str(extension)])
            try:
                worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker", timeout=10000)
                page = await context.new_page()
                await page.goto(url)
                await worker.evaluate("""async url => {
                    const tab=(await chrome.tabs.query({})).find(tab=>tab.url===url);
                    await chrome.scripting.insertCSS({target:{tabId:tab.id},files:['capture.css']});
                    await chrome.scripting.executeScript({target:{tabId:tab.id},files:['content.js']});
                }""", url)
                initial_hits = list(hits)
                for identifier in ("first", "second"):
                    await select_capture_target(page, page.locator('#' + identifier))
                    await page.get_by_role('button', name='Ask first', exact=True).click()
                await page.get_by_role('button', name='Remove target', exact=True).first.click()
                await select_capture_target(page, page.locator('#third'))
                await page.get_by_role('button', name='Ask first', exact=True).click()
                await expect(page.get_by_role('button', name='Remove target', exact=True)).to_have_count(2)
                assert hits == initial_hits, "Capturing/editing must never activate a website target"
                async with page.expect_download() as download:
                    await page.get_by_role('button', name='Download', exact=True).click()
                await (await download.value).save_as(exported)
            finally:
                await context.close()
        capture = json.loads(exported.read_text())
        targets = capture['targets']
        assert len(targets) == 2 and len({target['id'] for target in targets}) == 2
        assert {target['fingerprint']['id'] for target in targets} == {'second', 'third'}
        await asyncio.to_thread(cli, 'policy', 'import', str(exported))
        await asyncio.to_thread(cli, 'policy', 'import', str(exported))
        third = next(target['id'] for target in targets if target['fingerprint']['id'] == 'third')
        await asyncio.to_thread(cli, 'policy', 'configure', capture['id'], '--target', third, '--decision', 'block')
        await asyncio.to_thread(cli, 'policy', 'validate')
        assert len(Project(project).policies('default')) == 1
        await asyncio.to_thread(_terminal_flow, project, url, tmp_path)
        assert hits.count('/second') == 1 and '/first' not in hits and '/third' not in hits
    reviews = Journal(project).reviews()
    assert len(reviews) == 2 and {review['state'] for review in reviews} == {'approved', 'rejected'}


@pytest.mark.asyncio
async def test_legacy_dashboard_access_form_loads_authenticated_ui_without_exposing_key(tmp_path):
    # This is a synthetic local fixture credential, never a real account key.
    key = 'fixture-dashboard-access-key'
    errors = []
    origins = []
    app = create_app(paths=DashboardPaths(tmp_path / 'legacy-project'), token=key)
    async def observe_response(request, response):
        # Only inert route/origin/status; never capture form bodies or tokens.
        origins.append((request.method, request.path, request.headers.get('Origin'), response.status))
    app.on_response_prepare.append(observe_response)
    async with TestServer(app, host='127.0.0.1') as server:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, chromium_sandbox=True,
                args=['--password-store=basic', '--use-mock-keychain'])
            try:
                context = await browser.new_context()
                ui = await context.new_page()
                ui.on('pageerror', lambda error: errors.append(str(error)))
                await ui.goto(str(server.make_url('/')))
                assert key not in await ui.content()
                assert (await context.request.get(str(server.make_url('/api/bootstrap')))).status == 401
                await ui.get_by_label('Access key', exact=True).fill(key)
                await ui.get_by_role('button', name='Open dashboard', exact=True).click()
                assert not ui.url.endswith('/api/session'), origins
                await expect(ui.get_by_role('heading', name='Overview', exact=True)).to_be_visible()
                await ui.get_by_role('button', name='Configs', exact=True).click()
                await expect(ui.get_by_role('heading', name='Configs', exact=True)).to_be_visible()
                assert (await context.request.get(str(server.make_url('/api/bootstrap')))).status == 200
                assert not errors
                independent = await browser.new_context()
                denied = await independent.request.get(str(server.make_url('/api/bootstrap')))
                assert denied.status == 401 and key not in await denied.text()
                await independent.close()
                await context.close()
            finally:
                await browser.close()
