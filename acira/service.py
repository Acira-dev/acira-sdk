"""Authenticated shared loopback service; never the enforcement authority."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import webbrowser
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .journal import Journal
from .local import Registry, atomic_json, file_lock, state_dir
from .project import CATALOG, Project, PolicyConflict, SNAPSHOT_JSON_LIMIT, catalog_policy, digest, read_json

PROTOCOL = 1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise ValueError("Acira local service redirects are not trusted")


def descriptor() -> dict[str, Any] | None:
    path = state_dir() / "server.json"
    if not path.exists():
        return None
    if path.is_symlink() or (os.name != "nt" and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077)):
        raise PermissionError("Unsafe local server descriptor permissions; run acira doctor")
    data = read_json(path)
    url = urlsplit(data.get("url", ""))
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path or url.query or url.fragment or url.username or url.password:
        raise ValueError("Invalid loopback service descriptor")
    if data.get("protocol") != PROTOCOL:
        raise ValueError("Incompatible Acira service protocol; stop the old service and reopen with a compatible version")
    return data


def verified_descriptor() -> dict[str, Any] | None:
    data = descriptor()
    if not data:
        return None
    nonce = secrets.token_hex(24)
    try:
        # Prove server possession before sending any bearer credential.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(data["url"] + "/handshake?nonce=" + nonce, timeout=1) as response:
            raw = response.read(4097)
            if len(raw) > 4096:
                raise ValueError("Invalid Acira service handshake")
            result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid Acira service handshake")
        expected = hmac.new(data["proof_key"].encode(), nonce.encode(), hashlib.sha256).hexdigest()
        if result.get("protocol") != PROTOCOL or not hmac.compare_digest(str(result.get("proof", "")), expected):
            raise ValueError("The process on the recorded port is not the trusted Acira service")
        return data
    except (OSError, TimeoutError):
        return None


def admin_request(data: dict[str, Any], path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(data["url"] + path, data=json.dumps(body or {}).encode(),
                                    headers={"Authorization": "Bearer " + data["admin_key"], "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def open_dashboard(*, launch_browser: bool = True) -> str:
    root = state_dir()
    with file_lock(root / "launch.lock"):
        data = verified_descriptor()
        if not data:
            log = root / "server.log"
            with log.open("ab") as output:
                kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": output, "stderr": output}
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                else:
                    kwargs["start_new_session"] = True
                subprocess.Popen([sys.executable, "-m", "acira", "server", "serve"], **kwargs)
            if os.name != "nt":
                log.chmod(0o600)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                data = verified_descriptor()
                if data:
                    break
                time.sleep(0.1)
            if not data:
                raise RuntimeError(f"Acira service did not start; inspect {log}")
        ticket = admin_request(data, "/api/ticket")["ticket"]
    url = data["url"] + "/#ticket=" + ticket
    if launch_browser:
        webbrowser.open(url)
    return url


class Service:
    def __init__(self, *, registry: Registry | None = None) -> None:
        self.registry = registry or Registry()
        self.admin_key = secrets.token_urlsafe(32)
        self.sdk_key = secrets.token_urlsafe(32)
        self.proof_key = secrets.token_urlsafe(32)
        self.cookie_name = "acira_" + secrets.token_hex(6)
        self.sessions: dict[str, float] = {}
        self.tickets: dict[str, float] = {}
        self.pairings: dict[str, float] = {}
        self.extensions_path = self.registry.root / "extensions.json"
        self.stop = asyncio.Event()
        self.port = 0

    def project(self, project_id: str) -> Project:
        return Project(self.registry.get(project_id)["root"])

    def extensions(self) -> dict[str, Any]:
        return read_json(self.extensions_path) if self.extensions_path.exists() else {}

    def role(self, request: web.Request) -> str | None:
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if bearer and hmac.compare_digest(bearer, self.admin_key):
            return "admin"
        if bearer and hmac.compare_digest(bearer, self.sdk_key):
            return "sdk"
        if bearer:
            entry = self.extensions().get(hashlib.sha256(bearer.encode()).hexdigest())
            if entry:
                return "extension"
        cookie = request.cookies.get(self.cookie_name, "")
        if self.sessions.get(cookie, 0) > time.time():
            return "ui"
        return None

    def app(self) -> web.Application:
        @web.middleware
        async def boundary(request: web.Request, handler: Any) -> web.StreamResponse:
            try:
                expected_host = f"127.0.0.1:{self.port}"
                if request.host != expected_host:
                    raise web.HTTPForbidden(text="Invalid local host")
                origin = request.headers.get("Origin")
                extension_origin = bool(origin and origin.startswith("chrome-extension://"))
                public = request.path in {"/", "/handshake", "/api/session", "/api/pair"} or request.path.startswith("/assets/")
                role = self.role(request)
                if origin and origin != f"http://{expected_host}":
                    if not (extension_origin and (request.path in {"/api/pair", "/handshake"} or role == "extension" or request.method == "OPTIONS")):
                        raise web.HTTPForbidden(text="Invalid request origin")
                if request.method == "OPTIONS" and extension_origin:
                    response = web.Response(status=204)
                else:
                    if not public and role is None:
                        raise web.HTTPUnauthorized(text="Open Acira from the terminal to authenticate")
                    if role == "sdk" and request.path != "/api/heartbeat":
                        raise web.HTTPForbidden(text="SDK capability cannot access dashboard management")
                    if role == "extension" and request.path not in {"/api/destinations", "/api/captures"}:
                        raise web.HTTPForbidden(text="Extension capability cannot access this resource")
                    if role == "ui" and request.method not in {"GET", "HEAD"} and request.headers.get("X-Acira-UI") != "1":
                        raise web.HTTPForbidden(text="Missing request confirmation header")
                    response = await handler(request)
                if extension_origin:
                    response.headers["Access-Control-Allow-Origin"] = origin
                    response.headers["Vary"] = "Origin"
                    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
                    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["Referrer-Policy"] = "no-referrer"
                response.headers["Cache-Control"] = "no-store"
                response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
                return response
            except PolicyConflict as exc:
                return web.json_response({"error": str(exc)}, status=409)
            except (ValueError, KeyError, TypeError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
            except PermissionError:
                return web.json_response({"error": "Operating-system access denied. Check the configured project folders."}, status=403)
            except (OSError, sqlite3.Error):
                return web.json_response({"error": "Local storage is unavailable; repair it before continuing."}, status=503)

        app = web.Application(middlewares=[boundary], client_max_size=1024 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_static("/assets", str(files("acira_dashboard").joinpath("static")), show_index=False)
        app.router.add_get("/handshake", self.handshake)
        app.router.add_post("/api/session", self.session)
        app.router.add_post("/api/ticket", self.ticket)
        app.router.add_post("/api/stop", self.shutdown)
        app.router.add_post("/api/heartbeat", self.heartbeat)
        app.router.add_get("/api/projects", self.projects)
        app.router.add_get("/api/catalog", self.catalog)
        app.router.add_post("/api/catalog/preview", self.catalog_preview)
        app.router.add_post("/api/projects/{project}/policy-updates", self.policy_updates)
        app.router.add_get("/api/projects/{project}/policies", self.policies)
        app.router.add_post("/api/projects/{project}/policies", self.save_policy)
        app.router.add_get("/api/projects/{project}/revisions", self.revisions)
        app.router.add_get("/api/projects/{project}/revisions/{revision}", self.revision)
        app.router.add_post("/api/projects/{project}/rollback", self.rollback)
        app.router.add_get("/api/projects/{project}/events", self.events)
        app.router.add_get("/api/projects/{project}/events/export", self.export_events)
        app.router.add_get("/api/projects/{project}/reviews", self.reviews)
        app.router.add_get("/api/projects/{project}/artifacts/{artifact}", self.artifact)
        app.router.add_get("/api/projects/{project}/grants", self.grants)
        app.router.add_delete("/api/projects/{project}/grants/{grant}", self.revoke_grant)
        app.router.add_post("/api/projects/{project}/reviews/{review}/decision", self.decide)
        app.router.add_post("/api/projects/{project}/runs/{run}/control", self.control)
        app.router.add_get("/api/projects/{project}/configuration", self.configuration)
        app.router.add_put("/api/projects/{project}/configuration", self.configure)
        app.router.add_post("/api/projects/{project}/backtest", self.backtest)
        app.router.add_get("/api/projects/{project}/secret-references", self.secret_references)
        app.router.add_put("/api/projects/{project}/secret-references", self.save_secret_references)
        app.router.add_get("/api/destinations", self.destinations)
        app.router.add_post("/api/pairing", self.pairing)
        app.router.add_post("/api/pair", self.pair)
        app.router.add_get("/api/extensions", self.extension_list)
        app.router.add_delete("/api/extensions/{extension}", self.extension_revoke)
        app.router.add_post("/api/captures", self.capture)
        app.router.add_route("OPTIONS", "/{tail:.*}", self.options)
        return app

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(text=files("acira_dashboard").joinpath("static/workspace.html").read_text(encoding="utf-8"), content_type="text/html")

    async def options(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def handshake(self, request: web.Request) -> web.Response:
        nonce = request.query.get("nonce", "")
        if not 16 <= len(nonce) <= 128:
            raise ValueError("Invalid challenge")
        key = self.proof_key
        extension = request.query.get("extension")
        if extension:
            entry = next((entry for entry in self.extensions().values() if entry["id"] == extension), None)
            if entry is None:
                raise web.HTTPForbidden()
            key = entry["proof_key"]
        return web.json_response({"protocol": PROTOCOL, "proof": hmac.new(key.encode(), nonce.encode(), hashlib.sha256).hexdigest()})

    async def ticket(self, request: web.Request) -> web.Response:
        if self.role(request) != "admin":
            raise web.HTTPForbidden()
        key = secrets.token_urlsafe(32)
        self.tickets = {k: expiry for k, expiry in self.tickets.items() if expiry > time.time()}
        self.tickets[key] = time.time() + 60
        return web.json_response({"ticket": key})

    async def session(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.tickets.pop(str(body.get("ticket", "")), 0) < time.time():
            raise web.HTTPUnauthorized(text="Launch acira open again; this sign-in link expired")
        key = secrets.token_urlsafe(32)
        self.sessions[key] = time.time() + 12 * 3600
        response = web.json_response({"authenticated": True})
        response.set_cookie(self.cookie_name, key, httponly=True, samesite="Strict", max_age=12 * 3600)
        return response

    async def shutdown(self, request: web.Request) -> web.Response:
        if self.role(request) != "admin":
            raise web.HTTPForbidden()
        self.stop.set()
        return web.json_response({"stopping": True, "enforcement": "continues in each SDK"})

    async def heartbeat(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.registry.get(str(body["project_id"]))
        return web.json_response({"protocol": PROTOCOL, "connected": True})

    def project_summary(self, row: dict[str, str]) -> dict[str, Any]:
        project = Project(row["root"])
        try:
            document = project.document()
            runs = [run for run in Journal(project.root).runs() if run.get("dashboard_enabled", True)]
            profiles = []
            for name in document["agents"]:
                try:
                    snapshot = project.snapshot(name)
                    revision, error = snapshot.revision, None
                except (OSError, ValueError, TypeError) as exc:
                    revision, error = None, str(exc)
                active = [run for run in runs if run.get("agent") == name and run.get("state") not in {"ended", "failed", "disconnected"}]
                state = "Invalid configuration" if error else "Waiting for agent" if not active else "Failed to apply" if any(run.get("error") for run in active) else "Active" if all(run.get("revision") == revision for run in active) else "Applying"
                profiles.append({**document["agents"][name], "name": name, "revision": revision, "state": state, "error": error,
                                 "policy_roots": {kind: str(path) for kind, path in project.folders(name).items()}})
            return {**row, "agents": profiles, "runs": runs}
        except (OSError, ValueError) as exc:
            return {**row, "error": str(exc), "agents": [], "runs": []}

    async def projects(self, request: web.Request) -> web.Response:
        return web.json_response({"projects": [self.project_summary(row) for row in self.registry.projects()]})

    async def catalog(self, request: web.Request) -> web.Response:
        return web.json_response({"policies": [{"name": name, "filename": CATALOG[name] + ".json", "document": catalog_policy(name)} for name in CATALOG]})

    async def catalog_preview(self, request: web.Request) -> web.Response:
        from .catalog import policy
        body = await request.json()
        document = await asyncio.to_thread(policy, body["url"], body["name"])
        return web.json_response({"document":document,"filename":"catalog-"+digest({"id":document["id"]})[:16]+".json"})

    async def policy_updates(self, request: web.Request) -> web.Response:
        from .catalog import updated
        body = await request.json()
        project = self.project(request.match_info["project"])
        updates = []
        for current in project.policies(body.get("agent","default")):
            old = current["document"]
            if old.get("acira_source"):
                new = await asyncio.to_thread(updated, old)
            else:
                name = next((name for name in CATALOG if catalog_policy(name)["id"] == old["id"]), None)
                if name is None:
                    continue
                new = catalog_policy(name)
                new["acira_decisions"] = old.get("acira_decisions", new["acira_decisions"])
            if old != new:
                updates.append({**current,"proposed":new})
        return web.json_response({"updates":updates,"note":"Preview only. Explicit save is required for each policy."})

    async def policies(self, request: web.Request) -> web.Response:
        project = self.project(request.match_info["project"])
        agent = request.query.get("agent", "default")
        return web.json_response({"policies": project.policies(agent), "revision": project.snapshot(agent).revision})

    async def save_policy(self, request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(self.project(request.match_info["project"]).save_policy(body.get("agent", "default"), body["filename"], body["document"], expected=body.get("expected")))

    async def revisions(self, request: web.Request) -> web.Response:
        project = self.project(request.match_info["project"])
        agent = request.query.get("agent", "default")
        project.folders(agent)
        paths = sorted((project.root / ".acira/revisions" / agent).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        offset = max(0, int(request.query.get("offset", "0")))
        revisions = []
        for path in paths[offset:offset+50]:
            value = read_json(path, max_bytes=SNAPSHOT_JSON_LIMIT)
            revisions.append({"revision": value["revision"], "created_at": value["created_at"], "agent": agent, "document_count": len(value["documents"])})
        return web.json_response({"revisions": revisions, "next_offset": offset+50 if len(paths)>offset+50 else None})

    async def revision(self, request: web.Request) -> web.Response:
        import re
        project = self.project(request.match_info["project"])
        agent = request.query.get("agent", "default")
        project.folders(agent)
        revision = request.match_info["revision"]
        if not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ValueError("Invalid revision")
        return web.json_response(read_json(project.root / ".acira/revisions" / agent / (revision+".json"), max_bytes=SNAPSHOT_JSON_LIMIT))

    async def rollback(self, request: web.Request) -> web.Response:
        body = await request.json()
        project = self.project(request.match_info["project"])
        from .revisions import rollback
        return web.json_response(rollback(project, body.get("agent", "default"), body["revision"], expected=body["expected"]))

    async def events(self, request: web.Request) -> web.Response:
        project = self.project(request.match_info["project"])
        journal = Journal(project.root)
        limit = min(max(int(request.query.get("limit", "100")), 1), 1000)
        descending = request.query.get("order") == "newest"
        events = journal.events(run=request.query.get("run"), agent=request.query.get("agent"),
            after=int(request.query.get("after", "0")), limit=limit+1, descending=descending,
            before=int(request.query["before"]) if "before" in request.query else None,
            query=request.query.get("q", ""), kind=request.query.get("kind"))
        more = len(events) > limit
        events = events[:limit]
        return web.json_response({"events": events, "has_more": more,
            "next_cursor": events[-1]["sequence"] if more else None})

    async def export_events(self, request: web.Request) -> web.StreamResponse:
        journal = Journal(self.project(request.match_info["project"]).root)
        # Freeze the high-water mark: a busy run cannot make this export endless.
        through = journal.latest_sequence()
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson",
            "Content-Disposition": 'attachment; filename="acira-activity.jsonl"'})
        await response.prepare(request)
        cursor = 0
        while cursor < through:
            events = journal.events(after=cursor, through=through, limit=500,
                run=request.query.get("run"), query=request.query.get("q", ""),
                agent=request.query.get("agent"), kind=request.query.get("kind"))
            if not events:
                break
            await response.write(("".join(json.dumps(event, ensure_ascii=False)+"\n" for event in events)).encode())
            cursor = events[-1]["sequence"]
        await response.write_eof()
        return response

    async def reviews(self, request: web.Request) -> web.Response:
        return web.json_response({"reviews": Journal(self.project(request.match_info["project"]).root).reviews()})

    async def artifact(self, request: web.Request) -> web.Response:
        import re
        name = request.match_info["artifact"]
        if not re.fullmatch(r"[a-f0-9]{32}\.png", name):
            raise ValueError("Invalid image identifier")
        path = self.project(request.match_info["project"]).root / ".acira/artifacts" / name
        if path.is_symlink() or not path.is_file():
            raise web.HTTPNotFound()
        return web.Response(body=path.read_bytes(), content_type="image/png")

    async def grants(self, request: web.Request) -> web.Response:
        journal = Journal(self.project(request.match_info["project"]).root)
        with journal.db() as db:
            rows = db.execute("SELECT * FROM grants WHERE expires>?", (time.time(),)).fetchall()
        return web.json_response({"grants": [{"id": row["id"], "run": row["run"], "expires_at": row["expires"], **json.loads(row["payload"])} for row in rows]})

    async def revoke_grant(self, request: web.Request) -> web.Response:
        journal = Journal(self.project(request.match_info["project"]).root)
        with journal.db() as db:
            db.execute("DELETE FROM grants WHERE id=?", (request.match_info["grant"],))
        journal.event("grant_revoked", grant_id=request.match_info["grant"])
        return web.json_response({"revoked": True})

    async def decide(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("decision") not in {"approved", "rejected"}:
            raise ValueError("Choose approved or rejected")
        journal = Journal(self.project(request.match_info["project"]).root)
        accepted = journal.decide(request.match_info["review"], body["decision"], grant=body.get("grant") is True)
        return web.json_response({"accepted": accepted, "review": journal.review(request.match_info["review"])}, status=200 if accepted else 409)

    async def control(self, request: web.Request) -> web.Response:
        body = await request.json()
        journal = Journal(self.project(request.match_info["project"]).root)
        desired = journal.control(request.match_info["run"], body["desired"])
        journal.event("control_requested", run=request.match_info["run"], desired=desired)
        return web.json_response({"desired": desired, "state": "Awaiting acknowledgement"})

    async def configuration(self, request: web.Request) -> web.Response:
        project = self.project(request.match_info["project"])
        document = project.document()
        return web.json_response({"document": document, "hash": digest(document)})

    async def configure(self, request: web.Request) -> web.Response:
        from .revisions import save_configuration
        body = await request.json()
        return web.json_response(save_configuration(self.project(request.match_info["project"]), body["document"], expected=body["expected"]))

    async def backtest(self, request: web.Request) -> web.Response:
        from .replay import compare
        return web.json_response(await asyncio.to_thread(compare, self.project(request.match_info["project"]), await request.json()))

    async def secret_references(self, request: web.Request) -> web.Response:
        from .secret_references import describe
        return web.json_response(describe(self.project(request.match_info["project"]), request.query.get("agent", "default")))

    async def save_secret_references(self, request: web.Request) -> web.Response:
        from .secret_references import save
        body = await request.json()
        return web.json_response(save(self.project(request.match_info["project"]), body.get("agent", "default"), body))

    async def destinations(self, request: web.Request) -> web.Response:
        projects = []
        for row in self.registry.projects():
            try:
                project = self.project(row["id"])
                agents = list(project.document()["agents"])
                folders = {agent: project.folders(agent)["specific"] for agent in agents}
            except (ValueError, OSError):
                # A moved or broken project must not hide other capture destinations.
                continue
            projects.append({"id": row["id"], "name": row["name"], "agents": agents,
                             "capture_scopes": {agent: [name for name in agents if folders[name] == folders[agent]] for agent in agents}})
        return web.json_response({"projects": projects})

    async def pairing(self, request: web.Request) -> web.Response:
        code = secrets.token_urlsafe(12)
        self.pairings = {key: expiry for key, expiry in self.pairings.items() if expiry > time.time()}
        self.pairings[code] = time.time() + 120
        return web.json_response({"code": code, "expires_in": 120, "url": f"http://127.0.0.1:{self.port}"})

    async def pair(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.pairings.pop(str(body.get("code", "")), 0) < time.time():
            raise web.HTTPForbidden(text="Pairing code invalid or expired")
        token = secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode()).hexdigest()
        entries = self.extensions()
        entries[key] = {"id": key[:16], "name": "Capture extension", "created_at": time.time(), "proof_key": secrets.token_urlsafe(32)}
        atomic_json(self.extensions_path, entries, private=True)
        return web.json_response({"credential": token, "extension_id": entries[key]["id"], "proof_key": entries[key]["proof_key"], "capabilities": ["destinations:list", "captures:import"]})

    async def extension_list(self, request: web.Request) -> web.Response:
        return web.json_response({"extensions": [{k:v for k,v in entry.items() if k != "proof_key"} for entry in self.extensions().values()]})

    async def extension_revoke(self, request: web.Request) -> web.Response:
        entries = {key: value for key, value in self.extensions().items() if value["id"] != request.match_info["extension"]}
        atomic_json(self.extensions_path, entries, private=True)
        return web.json_response({"revoked": True})

    async def capture(self, request: web.Request) -> web.Response:
        body = await request.json()
        project = self.project(body["project_id"])
        agent = body["agent"]
        from .project import validate_policy
        value = body["document"]
        if validate_policy(value)[0] != "specific":
            raise ValueError("Extension can only import captured specific policies")
        filename = "capture-" + digest(value)[:20] + ".json"
        path = project.folders(agent)["specific"] / filename
        if path.exists() and read_json(path) == value:
            return web.json_response({"duplicate": True, "revision": project.snapshot(agent).revision})
        return web.json_response(project.save_policy(agent, filename, value, expected=None))


async def serve() -> None:
    root = state_dir()
    with file_lock(root / "server.lock", timeout=0.1):
        service = Service()
        runner = web.AppRunner(service.app(), access_log=None)
        await runner.setup()
        port_path = root / "server-port.json"
        preferred_port = read_json(port_path)["port"] if port_path.exists() else 0
        site = web.TCPSite(runner, "127.0.0.1", preferred_port)
        await site.start()
        service.port = site._server.sockets[0].getsockname()[1]
        atomic_json(port_path, {"port": service.port}, private=True)
        instance = secrets.token_hex(16)
        atomic_json(root / "server.json", {"protocol": PROTOCOL, "url": f"http://127.0.0.1:{service.port}", "instance": instance,
                     "admin_key": service.admin_key, "sdk_key": service.sdk_key, "proof_key": service.proof_key}, private=True)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, service.stop.set)
        try:
            await service.stop.wait()
        finally:
            await runner.cleanup()
            with contextlib.suppress(OSError, ValueError):
                if read_json(root / "server.json").get("instance") == instance:
                    (root / "server.json").unlink()
