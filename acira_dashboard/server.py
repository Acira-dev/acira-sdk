from __future__ import annotations

import argparse
import asyncio
import copy
import errno
import hmac
import html
import json
import os
import secrets
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.general_skills import GeneralSkill
from acira_cdp_proxy.redaction import redact_value
from acira_cdp_proxy.secrets import parse_secrets_config
from acira_cdp_proxy.skills import SpecificSkill

from .backtest import DashboardBacktestError, run_backtest
from .merged_skills import MergedSkillError, MergedSkillStore
from .runtime import DashboardRuntimeError, DashboardRuntimeStore, dashboard_redact
from .discovery import clear_descriptor, write_descriptor
from .store import DEFAULT_EVENT_FILE, DashboardEventStore


PACKAGE_DIR = Path(__file__).resolve().parents[1]
# Back-compat alias. Runtime paths are now scoped to the developer's project, not the package.
PRODUCTION_DIR = PACKAGE_DIR
SAFE_JSON_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SECRET_CONFIG_EXAMPLE = "acira.secrets.example.json"
SECRET_CONFIG_SUFFIX = ".secrets.local.json"
ENV_FILE_RELATIVE_DIR = "secrets"
ENV_FILE_FILENAME = "acira.secrets.env"
ENV_FILE_PROVIDER_ID = "local"
ENV_FILE_PROVIDER_PATH = f"{ENV_FILE_RELATIVE_DIR}/{ENV_FILE_FILENAME}"
CONFIG_REDACTED_KEYS = {"api_key", "authorization", "password", "plain_text", "plaintext", "secret", "token", "value", "values"}
SPECIFIC_FINGERPRINT_RAW_VALUE_KEYS = {"value", "value_sample", "input_value", "text_value", "raw_value"}


@dataclass(frozen=True)
class DashboardPaths:
    """Filesystem roots for the dashboard.

    ``production_dir`` is the developer's *project* directory (where ``acira dashboard``
    is run): its configs, skills, secrets and runtime live there — that is what makes the
    dashboard project-local. ``package_dir`` is the installed Acira package, which ships
    the static UI assets. The split lets the UI load from the package while all policy
    data is read from the project.
    """

    production_dir: Path = field(default_factory=Path.cwd)
    package_dir: Path = PACKAGE_DIR

    def __post_init__(self) -> None:
        if not isinstance(self.production_dir, Path):
            object.__setattr__(self, "production_dir", Path(self.production_dir))
        if not isinstance(self.package_dir, Path):
            object.__setattr__(self, "package_dir", Path(self.package_dir))

    # Project-scoped — the developer's project.
    @property
    def configs_dir(self) -> Path:
        return self.production_dir / "configs"

    @property
    def general_skills_dir(self) -> Path:
        return self.production_dir / "skills" / "general"

    @property
    def specific_skills_dir(self) -> Path:
        return self.production_dir / "skills" / "specific"

    @property
    def merged_skills_dir(self) -> Path:
        return self.production_dir / "skills" / "merged"

    @property
    def runtime_dir(self) -> Path:
        return self.production_dir / "local-runtime" / "dashboard"

    @property
    def event_file(self) -> Path:
        return self.runtime_dir / "events.jsonl"

    # Package-scoped — ships with Acira.
    @property
    def static_dir(self) -> Path:
        return self.package_dir / "acira_dashboard" / "static"


PATHS_KEY = web.AppKey("acira_dashboard_paths", DashboardPaths)
TOKEN_KEY = web.AppKey("acira_dashboard_token", str)
SESSIONS_KEY = web.AppKey("acira_dashboard_sessions", dict)
COOKIE_KEY = web.AppKey("acira_dashboard_cookie", str)
EVENT_STORE_KEY = web.AppKey("acira_dashboard_event_store", DashboardEventStore)
RUNTIME_STORE_KEY = web.AppKey("acira_dashboard_runtime_store", DashboardRuntimeStore)


def create_app(
    *,
    paths: DashboardPaths | None = None,
    token: str | None = None,
    event_store: DashboardEventStore | None = None,
) -> web.Application:
    selected_paths = paths or DashboardPaths()
    selected_token = token or secrets.token_urlsafe(32)
    selected_store = event_store or DashboardEventStore(selected_paths.event_file)
    selected_runtime = DashboardRuntimeStore(selected_paths.runtime_dir, event_store=selected_store)
    app = web.Application(middlewares=[_security_middleware])
    app[PATHS_KEY] = selected_paths
    app[TOKEN_KEY] = selected_token
    app[SESSIONS_KEY] = {}
    app[COOKIE_KEY] = "acira_legacy_" + secrets.token_hex(6)
    app[EVENT_STORE_KEY] = selected_store
    app[RUNTIME_STORE_KEY] = selected_runtime
    app.router.add_get("/", _handle_index)
    app.router.add_post("/api/session", _handle_auth_session)
    app.router.add_get("/static/{name}", _handle_static_asset)
    app.router.add_get("/api/bootstrap", _handle_bootstrap)
    app.router.add_get("/api/status", _handle_status)
    app.router.add_get("/api/configs", _handle_configs)
    app.router.add_post("/api/configs", _handle_config_create)
    app.router.add_get("/api/configs/{name}", _handle_config_get)
    app.router.add_put("/api/configs/{name}", _handle_config_put)
    app.router.add_delete("/api/configs/{name}", _handle_config_delete)
    app.router.add_get("/api/configs/{name}/secrets-config-link", _handle_config_secret_link_get)
    app.router.add_put("/api/configs/{name}/secrets-config-link", _handle_config_secret_link_put)
    app.router.add_get("/api/configs/{name}/graph", _handle_config_graph_get)
    app.router.add_put("/api/configs/{name}/graph", _handle_config_graph_put)
    app.router.add_get("/api/skills/status", _handle_skills_status)
    app.router.add_get("/api/skills/general", _handle_general_skills)
    app.router.add_put("/api/skills/general/{name}", _handle_general_skill_put)
    app.router.add_get("/api/skills/specific", _handle_specific_skills)
    app.router.add_post("/api/skills/specific/validate", _handle_specific_skill_validate)
    app.router.add_post("/api/skills/specific/import", _handle_specific_skill_import)
    app.router.add_get("/api/skills/specific/{name}", _handle_specific_skill_get)
    app.router.add_put("/api/skills/specific/{name}", _handle_specific_skill_put)
    app.router.add_delete("/api/skills/specific/{name}", _handle_specific_skill_delete)
    app.router.add_get("/api/skills/merged", _handle_merged_skills)
    app.router.add_get("/api/skills/merged/{name}", _handle_merged_skill_get)
    app.router.add_put("/api/skills/merged/{name}", _handle_merged_skill_put)
    app.router.add_delete("/api/skills/merged/{name}", _handle_merged_skill_delete)
    app.router.add_get("/api/sessions", _handle_sessions)
    app.router.add_get("/api/sessions/{session_id}", _handle_session_get)
    app.router.add_post("/api/sessions/{session_id}/pause", _handle_session_pause)
    app.router.add_post("/api/sessions/{session_id}/resume", _handle_session_resume)
    app.router.add_get("/api/reviews", _handle_reviews)
    app.router.add_get("/api/reviews/{review_id}", _handle_review_get)
    app.router.add_post("/api/reviews/{review_id}/decision", _handle_review_decision)
    app.router.add_get("/api/artifacts/{artifact_id}", _handle_artifact_get)
    app.router.add_post("/api/backtests", _handle_backtest)
    app.router.add_get("/api/secrets/configs", _handle_secret_configs)
    app.router.add_get("/api/secrets/configs/{name}", _handle_secret_config_get)
    app.router.add_put("/api/secrets/configs/{name}", _handle_secret_config_put)
    app.router.add_delete("/api/secrets/configs/{name}", _handle_secret_config_delete)
    app.router.add_put("/api/secrets/configs/{name}/secrets/{secret_id}", _handle_secret_config_record_put)
    app.router.add_delete("/api/secrets/configs/{name}/secrets/{secret_id}", _handle_secret_config_record_delete)
    app.router.add_get("/api/secrets", _handle_secrets)
    app.router.add_get("/api/secrets/{secret_id}", _handle_secret_get)
    app.router.add_put("/api/secrets/{secret_id}", _handle_secret_put)
    app.router.add_delete("/api/secrets/{secret_id}", _handle_secret_delete)
    app.router.add_post("/api/runtime/sessions", _handle_runtime_session_upsert)
    app.router.add_post("/api/runtime/sessions/{session_id}/end", _handle_runtime_session_end)
    app.router.add_get("/api/runtime/sessions/{session_id}/control", _handle_runtime_session_control)
    app.router.add_post("/api/runtime/events", _handle_runtime_event)
    app.router.add_post("/api/runtime/reviews", _handle_runtime_review_create)
    app.router.add_put("/api/runtime/reviews/{review_id}/artifact", _handle_runtime_review_artifact_put)
    app.router.add_get("/api/runtime/reviews/{review_id}/decision", _handle_runtime_review_decision)
    app.router.add_get("/api/events", _handle_events)
    app.router.add_get("/api/events/stream", _handle_event_stream)
    return app


async def run_dashboard(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    api_key: str | None = None,
    project_dir: str | Path | None = None,
) -> None:
    if not _is_loopback_host(host):
        raise ValueError("Acira dashboard may only bind to a loopback host.")
    resolved_project_dir = Path(project_dir) if project_dir is not None else Path.cwd()
    token = api_key or secrets.token_urlsafe(32)
    paths = DashboardPaths(production_dir=resolved_project_dir)
    app = create_app(paths=paths, token=token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except OSError:
        await runner.cleanup()
        raise
    bound_port = _resolve_bound_port(site) or port

    # Drop a discovery descriptor so agents in this project auto-connect (carries the token).
    descriptor_written = False
    try:
        write_descriptor(resolved_project_dir, host=host, port=bound_port, token=token)
        descriptor_written = True
    except OSError:
        descriptor_written = False

    print(f"Acira dashboard (project {resolved_project_dir}): http://{host}:{bound_port}/", flush=True)
    print(f"Dashboard access key (paste into the local sign-in form): {token}", flush=True)
    if descriptor_written:
        print("Agents in this project auto-discover the dashboard via .acira/dashboard.json", flush=True)
    try:
        await asyncio.Future()
    finally:
        if descriptor_written:
            clear_descriptor(resolved_project_dir)
        await runner.cleanup()


@web.middleware
async def _security_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if not _is_allowed_host_header(request.host):
        return _json_error("Only loopback hosts may access the Acira dashboard.", status=403)
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        return _json_error("Only this dashboard's origin may access it.", status=403)
    expected = str(request.app[TOKEN_KEY])
    provided = request.headers.get("X-Acira-Dashboard-Token", "")
    token_auth = bool(provided) and hmac.compare_digest(provided.encode(), expected.encode())
    sessions = request.app[SESSIONS_KEY]
    for key, expiry in list(sessions.items()):
        if expiry <= time.time():
            sessions.pop(key, None)
    cookie_auth = sessions.get(request.cookies.get(request.app[COOKIE_KEY], ""), 0) > time.time()
    if request.path.startswith("/api/") and request.path != "/api/session" and not (token_auth or cookie_auth):
        return _json_error("Authenticate with the local dashboard access key.", status=401)
    # Preserve the existing explicit mutation confirmation. Cookies authorize
    # reads, but a form from another page cannot silently change a project.
    if request.path != "/api/session" and request.method in MUTATING_METHODS and not token_auth:
        return _json_error("Missing or invalid dashboard token.", status=401)
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # Native sign-in POSTs must keep their same-origin Origin header. A
    # no-referrer policy turns that header into "null" for navigations.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


async def _handle_index(request: web.Request) -> web.Response:
    expected = str(request.app[TOKEN_KEY])
    provided = request.headers.get("X-Acira-Dashboard-Token", "")
    authenticated = (bool(provided) and hmac.compare_digest(provided.encode(), expected.encode())) or (
        request.app[SESSIONS_KEY].get(request.cookies.get(request.app[COOKIE_KEY], ""), 0) > time.time())
    if not authenticated:
        return web.Response(text='''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Sign in to Acira</title><h1>Acira Dashboard</h1>
<p>Enter the local dashboard access key shown in your terminal.</p>
<form method="post" action="/api/session" autocomplete="off">
<label>Access key <input type="password" name="token" autocomplete="off" required></label>
<button type="submit">Open dashboard</button></form></html>''', content_type="text/html")
    paths: DashboardPaths = request.app[PATHS_KEY]
    index_path = paths.static_dir / "index.html"
    page = index_path.read_text(encoding="utf-8")
    page = page.replace("__ACIRA_DASHBOARD_TOKEN__", html.escape(expected, quote=True))
    return web.Response(text=page, content_type="text/html")


async def _handle_auth_session(request: web.Request) -> web.Response:
    # Browser forms supply Origin. Direct API clients can use the existing token
    # header instead of exchanging it for a browser cookie.
    if request.headers.get("Origin") != f"{request.scheme}://{request.host}":
        return _json_error("Sign in from the local dashboard page.", status=403)
    form = request.content_type == "application/x-www-form-urlencoded"
    body = await request.post() if form else await request.json()
    provided = body.get("token") if hasattr(body, "get") else None
    if not isinstance(provided, str) or len(provided) > 4096 or not hmac.compare_digest(provided.encode(), request.app[TOKEN_KEY].encode()):
        return _json_error("Invalid dashboard access key.", status=401)
    sessions = request.app[SESSIONS_KEY]
    if len(sessions) >= 256:
        return _json_error("Too many dashboard sessions; wait for an existing session to expire.", status=429)
    key = secrets.token_urlsafe(32)
    sessions[key] = time.time() + 12 * 3600
    response = web.Response(status=303, headers={"Location": "/"}) if form else web.json_response({"authenticated": True})
    response.set_cookie(request.app[COOKIE_KEY], key, httponly=True, samesite="Strict", max_age=12 * 3600)
    return response


async def _handle_static_asset(request: web.Request) -> web.StreamResponse:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_static_asset_path(paths.static_dir, request.match_info["name"])
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def _handle_bootstrap(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    store: DashboardEventStore = request.app[EVENT_STORE_KEY]
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    event_summary = store.summary()
    payload = {
        "status": _status_payload(paths, store, runtime),
        "events": store.read_recent(limit=100),
        "attention": event_summary.get("latest_events", []),
        "sessions": runtime.list_sessions(),
        "reviews": runtime.list_reviews(status_filter="pending"),
        "configs": [_config_summary(path, paths) for path in _core_config_files(paths)],
        "secret_configs": [_secret_config_summary(path) for path in _secret_config_files(paths)],
        "skills": {
            "general": [_general_skill_summary(path) for path in _json_files(paths.general_skills_dir)],
            "specific": [_specific_skill_summary(path) for path in _json_files(paths.specific_skills_dir)],
            "merged": MergedSkillStore(production_dir=paths.production_dir).list(),
            "diagnostics": _skills_diagnostics(paths),
        },
    }
    return _json_response(payload)


async def _handle_status(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    store: DashboardEventStore = request.app[EVENT_STORE_KEY]
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    return _json_response(_status_payload(paths, store, runtime))


async def _handle_configs(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    configs = [_config_summary(path, paths) for path in _core_config_files(paths)]
    return _json_response({"configs": configs})


async def _handle_config_create(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        payload = await _request_json_object(request)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise DashboardInputError("Config file name is required.", 400)
        path = _safe_json_path(paths.configs_dir, name, require_exists=False)
        if path.exists() and not bool(payload.get("overwrite", False)):
            raise DashboardInputError("Config file already exists.", 409)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else _dashboard_config_template(paths, path)
        if _contains_inline_secret_value(data):
            raise DashboardInputError("Inline plaintext secrets cannot be written from the dashboard in this slice.", 400)
        valid, error = _validate_config_data(data, paths)
        if not valid:
            raise DashboardInputError(f"Invalid Acira config: {error}", 400)
        _atomic_write_json(path, data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"name": path.name, "data": _redact_config_data(data), "valid": True}, status=201)


async def _handle_config_get(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.configs_dir, request.match_info["name"])
        data = _read_json_object(path)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    valid, error = _validate_config_data(data, paths)
    return _json_response(
        {
            "name": path.name,
            "data": _redact_config_data(data),
            "valid": valid,
            "error": error,
        }
    )


async def _handle_config_put(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.configs_dir, request.match_info["name"], require_exists=False)
        incoming = await _request_json_object(request)
        existing = _read_json_object(path) if path.exists() else {}
        data = _restore_redacted_config_values(incoming, existing)
        if _contains_redacted_config_placeholder(data):
            raise DashboardInputError("Redacted config placeholders cannot be stored as plaintext values.", 400)
        if _contains_inline_secret_value(data):
            raise DashboardInputError("Inline plaintext secrets cannot be written from the dashboard in this slice.", 400)
        valid, error = _validate_config_data(data, paths)
        if not valid:
            raise DashboardInputError(f"Invalid Acira config: {error}", 400)
        _atomic_write_json(path, data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"name": path.name, "data": _redact_config_data(data), "valid": True})


async def _handle_config_delete(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.configs_dir, request.match_info["name"])
        name = path.name
        path.unlink(missing_ok=True)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    except OSError as exc:
        return _json_error(f"Could not delete config: {exc}", status=500)
    return _json_response({"deleted": True, "name": name})


async def _handle_config_secret_link_get(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.configs_dir, request.match_info["name"])
        data = _read_json_object(path)
        link = _current_secret_config_link(data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"name": path.name, "secrets_config_path": link, "configs": [_secret_config_summary(item) for item in _secret_config_files(paths)]})


async def _handle_config_secret_link_put(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.configs_dir, request.match_info["name"])
        data = _read_json_object(path)
        payload = await _request_json_object(request)
        link = payload.get("secrets_config_path", payload.get("name", ""))
        link_text = str(link or "").strip()
        data.pop("secrets_config_file", None)
        if link_text:
            _safe_secret_config_path(paths, link_text)
            data["secrets_config_path"] = link_text
        else:
            data.pop("secrets_config_path", None)
        valid, error = _validate_config_data(data, paths)
        if not valid:
            raise DashboardInputError(f"Invalid Acira config after linking secrets config: {error}", 400)
        _atomic_write_json(path, data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"name": path.name, "secrets_config_path": data.get("secrets_config_path"), "data": _redact_config_data(data), "valid": True})


async def _handle_config_graph_get(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.configs_dir, request.match_info["name"])
        graph = _graph_from_config(path)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"name": path.name, "graph": graph})


async def _handle_config_graph_put(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.configs_dir, request.match_info["name"])
        payload = await _request_json_object(request)
        graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else payload
        config_data = _read_json_object(path)
        compiled = _compile_config_graph(paths, graph)
        existing_general = config_data.get("general_skills") if isinstance(config_data.get("general_skills"), dict) else {}
        merged_general: dict[str, Any] = {}
        for skill_id, block in compiled["general_skills"].items():
            risks = dict(block.get("risks") or {})
            prev = existing_general.get(skill_id)
            if isinstance(prev, dict) and isinstance(prev.get("risks"), dict):
                risks.update(prev["risks"])  # preserve previously-configured per-skill decisions
            merged_general[skill_id] = {"path": block["path"], "risks": risks}
        config_data["general_skills"] = merged_general
        config_data["specific_skills"] = compiled["specific_skills"]
        config_data.pop("risk_policy", None)
        config_data.pop("page_risk_policy", None)
        config_data.pop("secrets_config_file", None)
        if compiled["secrets_config_path"]:
            config_data["secrets_config_path"] = compiled["secrets_config_path"]
        else:
            config_data.pop("secrets_config_path", None)
        valid, error = _validate_config_data(config_data, paths)
        if not valid:
            raise DashboardInputError(f"Invalid compiled config graph: {error}", 400)
        _atomic_write_json(path, config_data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    except MergedSkillError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"name": path.name, "graph": graph, "compiled": compiled, "config": _redact_config_data(config_data)})


async def _handle_general_skills(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    skills = [_general_skill_summary(path) for path in _json_files(paths.general_skills_dir)]
    return _json_response({"skills": skills, "read_only": True})


async def _handle_skills_status(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    return _json_response({"skills": _skills_diagnostics(paths)})


async def _handle_general_skill_put(request: web.Request) -> web.Response:
    return _json_error("General skills are built-in and read-only in this dashboard slice.", status=405)


async def _handle_specific_skills(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    skills = [_specific_skill_summary(path) for path in _json_files(paths.specific_skills_dir)]
    return _json_response({"skills": skills})


async def _handle_specific_skill_validate(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        payload = await _request_json_object(request)
        data = _specific_skill_data_from_payload(payload)
        data, removed_raw_value_fields = _sanitize_specific_skill_import_data(data)
        valid, error = _validate_specific_skill_data(data, paths)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response(
        {
            "valid": valid,
            "error": error,
            "data": redact_value(data),
            "normalized": removed_raw_value_fields > 0,
            "removed_raw_value_fields": removed_raw_value_fields,
        }
    )


async def _handle_specific_skill_import(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        payload = await _request_json_object(request)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise DashboardInputError("Specific skill file name is required.", 400)
        path = _safe_json_path(paths.specific_skills_dir, name, require_exists=False)
        if path.exists() and not bool(payload.get("overwrite", False)):
            raise DashboardInputError("Specific skill file already exists.", 409)
        data = _specific_skill_data_from_payload(payload)
        data, removed_raw_value_fields = _sanitize_specific_skill_import_data(data)
        valid, error = _validate_specific_skill_data(data, paths)
        if not valid:
            raise DashboardInputError(f"Invalid specific skill: {error}", 400)
        _atomic_write_json(path, data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response(
        {
            "name": path.name,
            "data": redact_value(data),
            "valid": True,
            "normalized": removed_raw_value_fields > 0,
            "removed_raw_value_fields": removed_raw_value_fields,
        },
        status=201,
    )


async def _handle_specific_skill_get(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.specific_skills_dir, request.match_info["name"])
        data = _read_json_object(path)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    valid, error = _validate_specific_skill_data(data, paths)
    return _json_response(
        {
            "name": path.name,
            "data": redact_value(data),
            "valid": valid,
            "error": error,
        }
    )


async def _handle_specific_skill_put(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.specific_skills_dir, request.match_info["name"], require_exists=False)
        data = await _request_json_object(request)
        valid, error = _validate_specific_skill_data(data, paths)
        if not valid:
            raise DashboardInputError(f"Invalid specific skill: {error}", 400)
        _atomic_write_json(path, data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"name": path.name, "data": redact_value(data), "valid": True})


async def _handle_specific_skill_delete(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_json_path(paths.specific_skills_dir, request.match_info["name"])
        references = _configs_referencing_specific_skill(paths, path.name)
        if references:
            names = ", ".join(references[:5])
            suffix = "..." if len(references) > 5 else ""
            raise DashboardInputError(f"Specific skill is still referenced by config(s): {names}{suffix}", 409)
        path.unlink()
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"deleted": True, "name": path.name})


async def _handle_merged_skills(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    return _json_response({"skills": MergedSkillStore(production_dir=paths.production_dir).list()})


async def _handle_merged_skill_get(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        payload = MergedSkillStore(production_dir=paths.production_dir).get(request.match_info["name"])
    except MergedSkillError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response(payload)


async def _handle_merged_skill_put(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        data = await _request_json_object(request)
        payload = MergedSkillStore(production_dir=paths.production_dir).put(request.match_info["name"], data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    except MergedSkillError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response(payload)


async def _handle_merged_skill_delete(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        MergedSkillStore(production_dir=paths.production_dir).delete(request.match_info["name"])
    except MergedSkillError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"deleted": True})


async def _handle_sessions(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    return _json_response({"sessions": runtime.list_sessions()})


async def _handle_session_get(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        payload = runtime.get_session(request.match_info["session_id"])
    except DashboardRuntimeError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response(payload)


async def _handle_session_pause(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _optional_json_object(request)
        session = runtime.set_session_paused(request.match_info["session_id"], paused=True, reason=data.get("reason"))
    except (DashboardInputError, DashboardRuntimeError) as exc:
        return _json_error(str(exc), status=getattr(exc, "status", 400))
    return _json_response({"session": session})


async def _handle_session_resume(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _optional_json_object(request)
        session = runtime.set_session_paused(request.match_info["session_id"], paused=False, reason=data.get("reason"))
    except (DashboardInputError, DashboardRuntimeError) as exc:
        return _json_error(str(exc), status=getattr(exc, "status", 400))
    return _json_response({"session": session})


async def _handle_reviews(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        reviews = runtime.list_reviews(
            status_filter=request.query.get("status", "all"),
            run_id=request.query.get("run_id"),
        )
    except DashboardRuntimeError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"reviews": reviews})


async def _handle_review_get(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        review = runtime.get_review(request.match_info["review_id"])
    except DashboardRuntimeError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"review": review})


async def _handle_review_decision(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _request_json_object(request)
        review = runtime.decide_review(
            request.match_info["review_id"],
            status=str(data.get("status") or data.get("decision") or ""),
            reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
        )
    except (DashboardInputError, DashboardRuntimeError) as exc:
        return _json_error(str(exc), status=getattr(exc, "status", 400))
    return _json_response({"review": review})


async def _handle_artifact_get(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        artifact = runtime.get_artifact(request.match_info["artifact_id"])
    except DashboardRuntimeError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"artifact": artifact})


async def _handle_backtest(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _request_json_object(request)
        payload = run_backtest(runtime_store=runtime, production_dir=paths.production_dir, request=data)
    except (DashboardInputError, DashboardRuntimeError, DashboardBacktestError, MergedSkillError) as exc:
        return _json_error(str(exc), status=getattr(exc, "status", 400))
    return _json_response(payload)


async def _handle_secret_configs(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    return _json_response({"configs": [_secret_config_summary(path) for path in _secret_config_files(paths)]})


async def _handle_secret_config_get(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_secret_config_path(paths, request.match_info["name"])
        data = _read_json_object(path)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    valid, error = _validate_secrets_config_data(data)
    return _json_response(
        {
            "name": path.name,
            "data": _redact_secrets_config_data(data),
            "read_only": not _is_writable_secret_config_name(path.name),
            "valid": valid,
            "error": error,
        }
    )


async def _handle_secret_config_put(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_secret_config_path(paths, request.match_info["name"], require_exists=False, writable=True)
        incoming = await _request_json_object(request)
        existing = _read_json_object(path) if path.exists() else {}
        data = _restore_redacted_secret_values(incoming, existing)
        if _contains_redacted_secret_placeholder(data):
            raise DashboardInputError("Redacted secret placeholders cannot be stored as plaintext values.", 400)
        _envify_secret_record_values(data, paths, path.name)
        valid, error = _validate_secrets_config_data(data)
        if not valid:
            raise DashboardInputError(f"Invalid secrets config: {error}", 400)
        _atomic_write_json(path, data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"name": path.name, "data": _redact_secrets_config_data(data), "valid": True, "read_only": False})


async def _handle_secret_config_delete(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_secret_config_path(paths, request.match_info["name"], writable=True)
        try:
            existing = _read_json_object(path)
            for record in _secret_records(existing):
                if isinstance(record, dict) and record.get("id"):
                    _purge_env_file_record(paths, path.name, str(record["id"]))
        except DashboardInputError:
            pass
        path.unlink()
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"deleted": True})


async def _handle_secret_config_record_put(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_secret_config_path(paths, request.match_info["name"], writable=True)
        data = _read_json_object(path)
        payload = await _request_json_object(request)
        record = _upsert_secret_record(data, request.match_info["secret_id"], payload)
        _envify_secret_record_values(data, paths, path.name)
        valid, error = _validate_secrets_config_data(data)
        if not valid:
            raise DashboardInputError(f"Invalid secrets config after record update: {error}", 400)
        _atomic_write_json(path, data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"name": path.name, "secret": _redact_secrets_config_data(record), "data": _redact_secrets_config_data(data), "valid": True})


async def _handle_secret_config_record_delete(request: web.Request) -> web.Response:
    paths: DashboardPaths = request.app[PATHS_KEY]
    try:
        path = _safe_secret_config_path(paths, request.match_info["name"], writable=True)
        data = _read_json_object(path)
        removed = _delete_secret_record(data, request.match_info["secret_id"])
        if not removed:
            raise DashboardInputError("Secret not found.", 404)
        _purge_env_file_record(paths, path.name, request.match_info["secret_id"])
        valid, error = _validate_secrets_config_data(data)
        if not valid:
            raise DashboardInputError(f"Invalid secrets config after record delete: {error}", 400)
        _atomic_write_json(path, data)
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"deleted": True, "name": path.name, "data": _redact_secrets_config_data(data)})


async def _handle_secrets(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    return _json_response({"secrets": runtime.list_secrets()})


async def _handle_secret_get(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        secret = runtime.get_secret(request.match_info["secret_id"])
    except DashboardRuntimeError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"secret": secret})


async def _handle_secret_put(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _request_json_object(request)
        data["id"] = request.match_info["secret_id"]
        secret = runtime.upsert_secret(data)
    except (DashboardInputError, DashboardRuntimeError) as exc:
        return _json_error(str(exc), status=getattr(exc, "status", 400))
    return _json_response({"secret": secret})


async def _handle_secret_delete(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        runtime.delete_secret(request.match_info["secret_id"])
    except DashboardRuntimeError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"deleted": True})


async def _handle_runtime_session_upsert(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _request_json_object(request)
        run_id = str(data.get("run_id") or data.get("session_id") or "").strip()
        if not run_id:
            raise DashboardInputError("Runtime session requires run_id.", 400)
        project_root = data.get("project_root")
        session = runtime.register_session(
            run_id,
            framework=str(data.get("framework") or "browser_use"),
            agent_name=str(data.get("agent_name")).strip() if isinstance(data.get("agent_name"), str) else None,
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
            project_root=str(project_root).strip() if isinstance(project_root, str) else None,
        )
    except (DashboardInputError, DashboardRuntimeError) as exc:
        return _json_error(str(exc), status=getattr(exc, "status", 400))
    return _json_response({"session": session})


async def _handle_runtime_session_end(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _optional_json_object(request)
        session = runtime.end_session(
            request.match_info["session_id"],
            reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
        )
    except (DashboardInputError, DashboardRuntimeError) as exc:
        return _json_error(str(exc), status=getattr(exc, "status", 400))
    return _json_response({"session": session})


async def _handle_runtime_session_control(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    store: DashboardEventStore = request.app[EVENT_STORE_KEY]
    wait_seconds = _bounded_wait_seconds(request.query.get("wait_seconds"))
    deadline = time.monotonic() + wait_seconds
    try:
        control = runtime.session_control(request.match_info["session_id"])
        initial_paused = bool(control.get("paused", False))
        initial_updated_at = str(control.get("updated_at") or control.get("last_heartbeat_at") or "")
        while initial_paused and time.monotonic() < deadline:
            revision = store.revision
            await store.wait_for_change_after(revision, timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            control = runtime.session_control(request.match_info["session_id"])
            if not bool(control.get("paused", False)):
                break
            updated_at = str(control.get("updated_at") or control.get("last_heartbeat_at") or "")
            if updated_at and updated_at != initial_updated_at:
                break
    except DashboardRuntimeError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response(control)


async def _handle_runtime_event(request: web.Request) -> web.Response:
    store: DashboardEventStore = request.app[EVENT_STORE_KEY]
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _request_json_object(request)
        event = data.get("event") if isinstance(data.get("event"), dict) else data
        appended = store.append(event)
        # Agent traffic proves liveness; keep the session live without a heartbeat loop.
        runtime.touch_session(event.get("run_id"))
    except DashboardInputError as exc:
        return _json_error(str(exc), status=exc.status)
    return _json_response({"event": appended})


async def _handle_runtime_review_create(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _request_json_object(request)
        review = runtime.create_review_from_payload(data)
    except (DashboardInputError, DashboardRuntimeError) as exc:
        return _json_error(str(exc), status=getattr(exc, "status", 400))
    return _json_response({"review": review})


async def _handle_runtime_review_artifact_put(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    try:
        data = await _request_json_object(request)
        payload = data.get("artifact") if isinstance(data.get("artifact"), dict) else data
        result = runtime.update_review_artifact(request.match_info["review_id"], payload)
    except (DashboardInputError, DashboardRuntimeError) as exc:
        return _json_error(str(exc), status=getattr(exc, "status", 400))
    return _json_response(result)


async def _handle_runtime_review_decision(request: web.Request) -> web.Response:
    runtime: DashboardRuntimeStore = request.app[RUNTIME_STORE_KEY]
    store: DashboardEventStore = request.app[EVENT_STORE_KEY]
    wait_seconds = _bounded_wait_seconds(request.query.get("wait_seconds"))
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            revision = store.revision
            payload = runtime.poll_review_decision(request.match_info["review_id"])
            if payload["status"] != "pending" or time.monotonic() >= deadline:
                return _json_response(payload)
            await store.wait_for_change_after(revision, timeout=min(0.1, max(0.0, deadline - time.monotonic())))
    except DashboardRuntimeError as exc:
        return _json_error(str(exc), status=exc.status)


async def _handle_events(request: web.Request) -> web.Response:
    store: DashboardEventStore = request.app[EVENT_STORE_KEY]
    limit = _query_limit(request)
    return _json_response({"events": store.read_recent(limit=limit)})


async def _handle_event_stream(request: web.Request) -> web.WebSocketResponse:
    store: DashboardEventStore = request.app[EVENT_STORE_KEY]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    events, offset, revision = store.read_recent_with_position(limit=50)
    if not await _send_ws_json(ws, {"type": "snapshot", "events": events}):
        return ws
    try:
        while not ws.closed:
            events, offset = store.read_since_offset(offset)
            for event in events:
                if not await _send_ws_json(ws, {"type": "event", "event": event}):
                    return ws
            revision = store.revision
            try:
                message = await ws.receive(timeout=0.01)
            except asyncio.TimeoutError:
                await store.wait_for_change_after(revision, timeout=0.5)
                continue
            if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break
    finally:
        await ws.close()
    return ws


async def _send_ws_json(ws: web.WebSocketResponse, payload: dict[str, Any]) -> bool:
    if ws.closed:
        return False
    try:
        await ws.send_json(payload)
        return True
    except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
        return False


class DashboardInputError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _status_payload(paths: DashboardPaths, store: DashboardEventStore, runtime: DashboardRuntimeStore) -> dict[str, Any]:
    event_summary = store.summary()
    session_summary = runtime.session_summary()
    review_summary = runtime.review_summary()
    return {
        "configs": {"count": len(_core_config_files(paths))},
        "skills": {
            "general_count": len(_json_files(paths.general_skills_dir)),
            "specific_count": len(_json_files(paths.specific_skills_dir)),
            "merged_count": len(_json_files(paths.merged_skills_dir)),
            "diagnostics": _skills_diagnostics(paths),
        },
        "events": {
            "count": event_summary["event_count"],
            "active_review_count": review_summary["pending_count"],
            "review_history_count": review_summary["history_count"],
            "review_total_count": review_summary["total_count"],
            "run_count": event_summary["run_count"],
            "paused_session_count": event_summary.get("paused_session_count", 0),
        },
        "sessions": {
            "count": session_summary["live_count"],
            **session_summary,
        },
        "secrets": {"config_count": len(_secret_config_files(paths)), "legacy_count": len(runtime.list_secrets())},
    }


def _config_summary(path: Path, paths: DashboardPaths) -> dict[str, Any]:
    try:
        data = _read_json_object(path)
        valid, error = _validate_config_data(data, paths)
        display_name = str(data.get("name") or path.stem)
        enabled = bool(data.get("enabled", True))
    except Exception as exc:
        return {"name": path.name, "display_name": path.stem, "valid": False, "error": str(exc)}
    return {
        "name": path.name,
        "display_name": display_name,
        "enabled": enabled,
        "valid": valid,
        "error": error,
    }


def _dashboard_config_template(paths: DashboardPaths, path: Path) -> dict[str, Any]:
    default_path = paths.configs_dir / "acira.default.json"
    try:
        data = _read_json_object(default_path) if default_path.exists() else {}
    except DashboardInputError:
        data = {}
    if not data:
        data = {
            "schema": "acira.config.v1",
            "enabled": True,
            "terminal_review": True,
            "terminal_log": True,
            "default_review_decision": "block",
            "adapters": {"browser_use": {"enabled": True, "trusted_helpers": ["overlay", "setup_overlay"], "hitl_freeze_mode": "strict"}},
            "general_skill_preflight": {"enabled": True, "quiet_ms": 750, "max_wait_ms": 5000, "on_error": "block_mutating_until_action_probe"},
            "agent_js": {"block_arbitrary": True},
            "general_skills": {},
        }
    data = json.loads(json.dumps(data))
    display_name = path.stem.replace("-", " ").replace("_", " ").strip().title() or path.stem
    data["schema"] = "acira.config.v1"
    data["name"] = display_name
    data["enabled"] = bool(data.get("enabled", True))
    dashboard = data.get("dashboard") if isinstance(data.get("dashboard"), dict) else {}
    data["dashboard"] = {
        "enabled": False,
        "base_url": str(dashboard.get("base_url") or dashboard.get("url") or "http://127.0.0.1:8765"),
        "review_timeout_seconds": dashboard.get("review_timeout_seconds", 3600),
        "heartbeat_seconds": dashboard.get("heartbeat_seconds", 2),
    }
    # A freshly created config is a clean slate: the user composes it by dragging
    # skills onto the canvas. (Templating off acira.default previously pre-filled every
    # skill, which made "adding" skills impossible and confusing.)
    data["general_skills"] = {}
    data["specific_skills"] = []
    data.pop("risk_policy", None)
    data.pop("page_risk_policy", None)
    data.pop("secrets_config_path", None)
    data.pop("secrets_config_file", None)
    data.setdefault("adapters", {"browser_use": {"enabled": True}})
    return data


def _core_config_files(paths: DashboardPaths) -> list[Path]:
    return [path for path in _json_files(paths.configs_dir) if not _is_secret_config_file(path)]


def _is_secret_config_file(path: Path) -> bool:
    name = path.name.lower()
    return name == "acira.secrets.example.json" or name.endswith(".secrets.local.json")


def _general_skill_summary(path: Path) -> dict[str, Any]:
    try:
        skill = GeneralSkill.from_file(path)
        return {
            "name": path.name,
            "id": skill.id,
            "display_name": skill.name,
            "version": skill.version,
            "risk_count": len(skill.emits),
            "risks": [emit.risk for emit in skill.emits],
            "valid": True,
            "error": None,
        }
    except Exception as exc:
        return {"name": path.name, "display_name": path.stem, "valid": False, "error": str(exc)}


def _specific_skill_summary(path: Path) -> dict[str, Any]:
    try:
        skill = SpecificSkill.from_file(path)
        return {
            "name": path.name,
            "id": skill.id,
            "display_name": skill.name,
            "version": skill.version,
            "target_count": len(skill.targets),
            "url_patterns": list(skill.url_patterns),
            "valid": True,
            "error": None,
        }
    except Exception as exc:
        return {"name": path.name, "display_name": path.stem, "valid": False, "error": str(exc)}


def _skills_diagnostics(paths: DashboardPaths) -> dict[str, Any]:
    def info(root: Path) -> dict[str, Any]:
        files = _json_files(root)
        return {
            "dir": str(root),
            "exists": root.exists(),
            "count": len(files),
            "files": [path.name for path in files],
        }

    return {
        "general": info(paths.general_skills_dir),
        "specific": info(paths.specific_skills_dir),
        "merged": info(paths.merged_skills_dir),
    }


def _validate_config_data(data: dict[str, Any], paths: DashboardPaths) -> tuple[bool, str | None]:
    try:
        AciraGuardConfig.from_dict(data, base_path=paths.configs_dir)
    except Exception as exc:
        return False, str(exc)
    return True, None


def _validate_specific_skill_data(data: dict[str, Any], paths: DashboardPaths) -> tuple[bool, str | None]:
    try:
        SpecificSkill.from_dict(data, base_path=paths.specific_skills_dir)
    except Exception as exc:
        return False, str(exc)
    return True, None


def _specific_skill_data_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if "data" in payload else payload
    if not isinstance(data, dict):
        raise DashboardInputError("Specific skill import must be a JSON object.", 400)
    return data


def _sanitize_specific_skill_import_data(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    cleaned = copy.deepcopy(data)
    removed = 0
    targets = cleaned.get("targets")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, dict):
                continue
            for key in ("fingerprint", "match"):
                fingerprint = target.get(key)
                if isinstance(fingerprint, dict):
                    removed += _strip_specific_fingerprint_raw_values(fingerprint)
    return cleaned, removed


def _strip_specific_fingerprint_raw_values(value: Any) -> int:
    removed = 0
    if isinstance(value, dict):
        for key in list(value.keys()):
            child = value[key]
            if str(key).lower() in SPECIFIC_FINGERPRINT_RAW_VALUE_KEYS:
                value.pop(key, None)
                removed += 1
                continue
            removed += _strip_specific_fingerprint_raw_values(child)
    elif isinstance(value, list):
        for child in value:
            removed += _strip_specific_fingerprint_raw_values(child)
    return removed


def _configs_referencing_specific_skill(paths: DashboardPaths, skill_name: str) -> list[str]:
    references: list[str] = []
    for path in _json_files(paths.configs_dir):
        try:
            data = _read_json_object(path)
        except DashboardInputError:
            continue
        if _specific_skill_refs_include(data, skill_name):
            references.append(path.name)
    return references


def _specific_skill_refs_include(data: dict[str, Any], skill_name: str) -> bool:
    candidates: list[str] = []
    for key in ("specific_skills", "specific_skill_paths"):
        candidates.extend(_string_list(data.get(key)))
    for item in candidates:
        if Path(str(item)).name == skill_name or str(item).strip() == skill_name:
            return True
    return False


def _validate_secrets_config_data(data: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        parse_secrets_config(data)
    except Exception as exc:
        return False, str(exc)
    return True, None


def _secret_config_files(paths: DashboardPaths) -> list[Path]:
    files = [path for path in _json_files(paths.configs_dir) if path.name.endswith(SECRET_CONFIG_SUFFIX)]
    example = paths.configs_dir / SECRET_CONFIG_EXAMPLE
    if example.exists() and example not in files:
        files.append(example)
    return sorted(files, key=lambda path: (not path.name.endswith(SECRET_CONFIG_SUFFIX), path.name))


def _secret_config_summary(path: Path) -> dict[str, Any]:
    try:
        data = _read_json_object(path)
        valid, error = _validate_secrets_config_data(data)
        records = _secret_records(data)
        return {
            "name": path.name,
            "display_name": path.stem,
            "enabled": bool(data.get("enabled", False)),
            "secret_count": len(records),
            "read_only": not _is_writable_secret_config_name(path.name),
            "valid": valid,
            "error": error,
        }
    except Exception as exc:
        return {
            "name": path.name,
            "display_name": path.stem,
            "read_only": not _is_writable_secret_config_name(path.name),
            "valid": False,
            "error": str(exc),
        }


def _safe_secret_config_path(
    paths: DashboardPaths,
    name: str,
    *,
    require_exists: bool = True,
    writable: bool = False,
) -> Path:
    path = _safe_json_path(paths.configs_dir, name, require_exists=require_exists)
    if path.name != SECRET_CONFIG_EXAMPLE and not path.name.endswith(SECRET_CONFIG_SUFFIX):
        raise DashboardInputError(f"Secrets config names must end with {SECRET_CONFIG_SUFFIX}.", 400)
    if writable and not _is_writable_secret_config_name(path.name):
        raise DashboardInputError(f"Only {SECRET_CONFIG_SUFFIX} secrets configs can be modified.", 405)
    return path


def _is_writable_secret_config_name(name: str) -> bool:
    return str(name or "").endswith(SECRET_CONFIG_SUFFIX)


def _current_secret_config_link(data: dict[str, Any]) -> str | None:
    value = data.get("secrets_config_path") or data.get("secrets_config_file")
    if isinstance(value, str) and value.strip():
        return value.strip()
    inline = data.get("secrets")
    if isinstance(inline, dict):
        nested = inline.get("config_file") or inline.get("file") or inline.get("path")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def _secret_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    records = data.get("secrets")
    if records is None:
        records = data.get("items")
    if records is None:
        records = data.get("entries")
    return records if isinstance(records, list) else []


def _secret_records_key(data: dict[str, Any]) -> str:
    if isinstance(data.get("secrets"), list):
        return "secrets"
    if isinstance(data.get("items"), list):
        return "items"
    if isinstance(data.get("entries"), list):
        return "entries"
    data["secrets"] = []
    return "secrets"


def _upsert_secret_record(data: dict[str, Any], secret_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    safe_id = _safe_identifier(secret_id)
    key = _secret_records_key(data)
    records = _secret_records(data)
    existing = next((item for item in records if isinstance(item, dict) and item.get("id") == safe_id), None)
    record = dict(existing or {})
    record["id"] = safe_id
    record["label"] = str(payload.get("label") or record.get("label") or safe_id)
    record["active"] = bool(payload.get("active", record.get("active", True)))
    record["tags"] = [str(item) for item in payload.get("tags", record.get("tags", [])) if str(item)] if isinstance(payload.get("tags", record.get("tags", [])), list) else []
    classification = payload.get("classification", payload.get("class", record.get("classification")))
    if classification is not None:
        record["classification"] = str(classification)
    policy = payload.get("policy", record.get("policy", {"decision": "ask"}))
    if isinstance(policy, dict):
        record["policy"] = policy
    elif isinstance(policy, str):
        record["policy"] = {"decision": policy}
    value = payload.get("value", payload.get("plaintext", payload.get("plain_text")))
    if isinstance(value, str) and value != "":
        record["value"] = value
    elif existing is None and not (record.get("provider") or record.get("provider_id") or record.get("ref") or record.get("provider_ref")):
        raise DashboardInputError("Plaintext value is required for new local secrets.", 400)
    if existing is None:
        records.append(record)
    else:
        index = records.index(existing)
        records[index] = record
    data[key] = records
    data.setdefault("enabled", True)
    data.setdefault("default_decision", "ask")
    return record


def _delete_secret_record(data: dict[str, Any], secret_id: str) -> bool:
    safe_id = _safe_identifier(secret_id)
    key = _secret_records_key(data)
    records = _secret_records(data)
    kept = [item for item in records if not (isinstance(item, dict) and item.get("id") == safe_id)]
    if len(kept) == len(records):
        return False
    data[key] = kept
    return True


def _restore_redacted_secret_values(incoming: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(json.dumps(incoming))
    existing_by_id = {
        str(item.get("id")): item
        for item in _secret_records(existing)
        if isinstance(item, dict) and item.get("id")
    }
    for record in _secret_records(data):
        if not isinstance(record, dict):
            continue
        existing_record = existing_by_id.get(str(record.get("id")))
        if not isinstance(existing_record, dict):
            continue
        for key in ("value", "plaintext", "plain_text"):
            if record.get(key) == "[REDACTED]" and key in existing_record:
                record[key] = existing_record[key]
    return data


def _contains_redacted_secret_placeholder(data: dict[str, Any]) -> bool:
    for record in _secret_records(data):
        if not isinstance(record, dict):
            continue
        if any(record.get(key) == "[REDACTED]" for key in ("value", "plaintext", "plain_text")):
            return True
    return False


def _redact_secrets_config_data(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_secrets_config_data(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secrets_config_data(item) for item in value]
    if not isinstance(value, dict):
        return value
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text.lower() in {"value", "values", "plaintext", "plain_text", "api_key", "token", "password", "authorization"}:
            redacted[key_text] = "[REDACTED]"
        else:
            redacted[key_text] = _redact_secrets_config_data(item)
    return redacted


def _env_file_path(paths: DashboardPaths) -> Path:
    return paths.configs_dir / ENV_FILE_RELATIVE_DIR / ENV_FILE_FILENAME


def _ensure_env_file_provider(data: dict[str, Any]) -> None:
    providers = data.get("providers")
    normalized: dict[str, Any] = dict(providers) if isinstance(providers, dict) else {}
    if isinstance(providers, list):
        for item in providers:
            if isinstance(item, dict) and item.get("id"):
                normalized[str(item["id"])] = {k: v for k, v in item.items() if k != "id"}
    normalized[ENV_FILE_PROVIDER_ID] = {"kind": "env_file", "env_path": ENV_FILE_PROVIDER_PATH}
    data["providers"] = normalized
    data.setdefault("enabled", True)
    data.setdefault("default_decision", "ask")


def _env_ref(secret_id: str) -> str:
    return secret_id.upper()


def _write_env_file(path: Path, env: dict[str, str]) -> None:
    _private_env_path(path)
    if any("\n" in value or "\r" in value or "\x00" in value for value in env.values()):
        raise DashboardInputError("Secret values must fit on a single environment-file line.", 400)
    lines = [f"{k}={v}" for k, v in env.items()]
    content = "\n".join(lines) + ("\n" if lines else "")
    fd, temporary = tempfile.mkstemp(prefix=".acira-secret-", dir=path.parent)
    tmp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _private_env_path(path)
        tmp_path.replace(path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        tmp_path.unlink(missing_ok=True)


def _private_env_path(path: Path) -> None:
    for directory in (path.parent.parent, path.parent):
        if directory.is_symlink():
            raise DashboardInputError("Secret storage cannot use symbolic-link directories.", 403)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or (os.name != "nt" and info.st_uid != os.getuid()):
            raise DashboardInputError("Secret storage must be owned by the current user.", 403)
    if os.name != "nt":
        path.parent.chmod(0o700)
    if path.is_symlink():
        raise DashboardInputError("Secret storage cannot use a symbolic-link file.", 403)
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_uid != os.getuid()):
            raise DashboardInputError("Secret storage must be a regular file owned by the current user.", 403)


def _read_env_file(path: Path) -> dict[str, str]:
    _private_env_path(path)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if k:
            result[k] = v
    return result


def _envify_secret_record_values(data: dict[str, Any], paths: DashboardPaths, config_name: str) -> None:
    records = _secret_records(data)
    if not records:
        return
    env_path = _env_file_path(paths)
    env = _read_env_file(env_path)
    touched = False
    for record in records:
        if not isinstance(record, dict):
            continue
        value = record.get("value", record.get("plaintext", record.get("plain_text")))
        if not isinstance(value, str) or value in ("", "[REDACTED]"):
            continue
        secret_id = _safe_identifier(str(record.get("id") or ""))
        ref = _env_ref(secret_id)
        env[ref] = value
        for key in ("value", "plaintext", "plain_text"):
            record.pop(key, None)
        record["provider_id"] = ENV_FILE_PROVIDER_ID
        record["provider_ref"] = ref
        touched = True
    if touched:
        _ensure_env_file_provider(data)
        valid, error = _validate_secrets_config_data(data)
        if not valid:
            raise DashboardInputError("Invalid secrets configuration.", 400)
        _write_env_file(env_path, env)


def _purge_env_file_record(paths: DashboardPaths, config_name: str, secret_id: str) -> None:
    env_path = _env_file_path(paths)
    env = _read_env_file(env_path)
    ref = _env_ref(_safe_identifier(secret_id))
    if ref in env:
        del env[ref]
        _write_env_file(env_path, env)


def _graph_from_config(config_path: Path) -> dict[str, Any]:
    data = _read_json_object(config_path)
    raw_general = data.get("general_skills")
    if isinstance(raw_general, dict):
        general = [str(block.get("path")) for block in raw_general.values() if isinstance(block, dict) and block.get("path")]
    else:
        general = _string_list(raw_general or data.get("general_skill_paths"))
    specific = _string_list(data.get("specific_skills") or data.get("specific_skill_paths"))
    secrets_config_path = _current_secret_config_link(data)
    nodes = [{"id": "config", "type": "config", "label": data.get("name") or config_path.name}]
    edges: list[dict[str, str]] = []
    for index, path in enumerate(general):
        node_id = f"general-{index}"
        nodes.append({"id": node_id, "type": "skill", "skill_type": "general", "name": Path(path).name, "path": path})
        edges.append({"from": node_id, "to": "config"})
    for index, path in enumerate(specific):
        node_id = f"specific-{index}"
        nodes.append({"id": node_id, "type": "skill", "skill_type": "specific", "name": Path(path).name, "path": path})
        edges.append({"from": node_id, "to": "config"})
    if secrets_config_path:
        node_id = "secret-config"
        name = Path(secrets_config_path).name
        nodes.append({"id": node_id, "type": "secret_config", "name": name, "path": secrets_config_path})
        edges.append({"from": node_id, "to": "config"})
    return {
        "schema": "acira.config_graph.v1",
        "config": config_path.name,
        "nodes": nodes,
        "edges": edges,
        "general_skills": [Path(path).name for path in general],
        "specific_skills": [Path(path).name for path in specific],
        "merged_skills": [],
        "secrets_config_path": secrets_config_path,
    }


def _compile_config_graph(paths: DashboardPaths, graph: dict[str, Any]) -> dict[str, Any]:
    general = _graph_skill_names(graph, "general")
    specific = _graph_skill_names(graph, "specific")
    merged = _graph_skill_names(graph, "merged")
    secrets_config_path = _graph_secret_config_name(graph)
    for name in general:
        _validate_graph_component_exists(paths.general_skills_dir, name, "general")
    for name in specific:
        _validate_graph_component_exists(paths.specific_skills_dir, name, "specific")
    for name in merged:
        _validate_graph_component_exists(paths.merged_skills_dir, name, "merged")
    if secrets_config_path:
        _safe_secret_config_path(paths, secrets_config_path)
    # Build per-skill general_skills blocks keyed by skill id. Direct components start with no
    # decisions (preserved/merged by the caller); merged components contribute their decisions.
    general_blocks: dict[str, dict[str, Any]] = {}
    for name in general:
        safe = _safe_component_name(name)
        skill = GeneralSkill.from_file(paths.general_skills_dir / safe)
        general_blocks.setdefault(skill.id, {"path": f"../skills/general/{safe}", "risks": {}})
    specific_refs = [f"../skills/specific/{_safe_component_name(name)}" for name in specific]
    if merged:
        compiled = MergedSkillStore(production_dir=paths.production_dir).expand_config_skills(merged)
        for skill_id, block in compiled["general_skills"].items():
            existing = general_blocks.get(skill_id)
            if existing is None:
                general_blocks[skill_id] = {"path": block["path"], "risks": dict(block.get("risks") or {})}
            else:
                existing["risks"].update(block.get("risks") or {})
        specific_refs.extend(item["path"] for item in compiled["specific_skills"])
    return {
        "general_skills": general_blocks,
        "specific_skills": _dedupe(specific_refs),
        "secrets_config_path": secrets_config_path,
    }


def _graph_skill_names(graph: dict[str, Any], skill_type: str) -> list[str]:
    key = f"{skill_type}_skills"
    result = [str(item) for item in graph.get(key, []) if isinstance(item, str) and item]
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") != "skill" or node.get("skill_type") != skill_type:
            continue
        name = node.get("name") or (Path(str(node.get("path"))).name if node.get("path") else None)
        if isinstance(name, str) and name:
            result.append(name)
    return _dedupe([_safe_component_name(item) for item in result])


def _validate_graph_component_exists(root: Path, name: str, skill_type: str) -> None:
    safe_name = _safe_component_name(name)
    path = (root / safe_name).resolve()
    if path.parent != root.resolve() or not path.exists() or not path.is_file():
        raise DashboardInputError(f"Unknown {skill_type} skill component: {safe_name}", 400)


def _graph_secret_config_name(graph: dict[str, Any]) -> str | None:
    candidates: list[str] = []
    direct = graph.get("secrets_config_path") or graph.get("secret_config_path") or graph.get("secret_config")
    if isinstance(direct, str) and direct.strip():
        candidates.append(Path(direct.strip()).name)
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "secret_config":
            continue
        name = node.get("name") or (Path(str(node.get("path"))).name if node.get("path") else None)
        if isinstance(name, str) and name.strip():
            candidates.append(Path(name.strip()).name)
    names = _dedupe(candidates)
    if len(names) > 1:
        raise DashboardInputError("A config graph can link only one secrets config.", 400)
    return names[0] if names else None


def _safe_component_name(name: str) -> str:
    text = str(name or "").strip()
    if "/" in text or "\\" in text or ".." in text or not text.endswith(".json"):
        raise DashboardInputError("Invalid skill component name.", 400)
    if any(char not in SAFE_JSON_NAME_CHARS for char in text):
        raise DashboardInputError("Invalid skill component name.", 400)
    return text


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str) and item]
    return []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _redact_config_data(data: dict[str, Any]) -> dict[str, Any]:
    return dashboard_redact(redact_value(_redact_inline_secrets(data)))


def _restore_redacted_config_values(incoming: Any, existing: Any) -> Any:
    if isinstance(incoming, list):
        existing_list = existing if isinstance(existing, list) else []
        return [
            _restore_redacted_config_values(item, existing_list[index] if index < len(existing_list) else None)
            for index, item in enumerate(incoming)
        ]
    if not isinstance(incoming, dict):
        return incoming
    existing_dict = existing if isinstance(existing, dict) else {}
    data: dict[str, Any] = {}
    for key, value in incoming.items():
        key_text = str(key)
        if key_text.lower() in CONFIG_REDACTED_KEYS and value == "[REDACTED]" and key_text in existing_dict:
            data[key_text] = existing_dict[key_text]
        else:
            data[key_text] = _restore_redacted_config_values(value, existing_dict.get(key_text))
    return data


def _contains_redacted_config_placeholder(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_redacted_config_placeholder(item) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if str(key).lower() in CONFIG_REDACTED_KEYS and item == "[REDACTED]":
            return True
        if _contains_redacted_config_placeholder(item):
            return True
    return False


def _redact_inline_secrets(value: Any, *, in_secrets: bool = False) -> Any:
    if isinstance(value, list):
        return [_redact_inline_secrets(item, in_secrets=in_secrets) for item in value]
    if not isinstance(value, dict):
        return value
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        next_in_secrets = in_secrets or key_text in {"secrets", "items", "entries"}
        if next_in_secrets and key_text in {"value", "values"}:
            redacted[key_text] = "[REDACTED]"
        else:
            redacted[key_text] = _redact_inline_secrets(item, in_secrets=next_in_secrets)
    return redacted


def _contains_inline_secret_value(value: Any, *, in_secrets: bool = False) -> bool:
    if isinstance(value, list):
        return any(_contains_inline_secret_value(item, in_secrets=in_secrets) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        key_text = str(key)
        next_in_secrets = in_secrets or key_text in {"secrets", "items", "entries"}
        if next_in_secrets and key_text in {"value", "values"}:
            return True
        if _contains_inline_secret_value(item, in_secrets=next_in_secrets):
            return True
    return False


async def _request_json_object(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except json.JSONDecodeError as exc:
        raise DashboardInputError(f"Invalid JSON: {exc}", 400) from exc
    except Exception as exc:
        raise DashboardInputError("Request body must be JSON.", 400) from exc
    if isinstance(data, dict) and isinstance(data.get("data"), dict) and len(data) == 1:
        data = data["data"]
    if not isinstance(data, dict):
        raise DashboardInputError("Request body must be a JSON object.", 400)
    return data


async def _optional_json_object(request: web.Request) -> dict[str, Any]:
    if request.content_length in {None, 0}:
        return {}
    return await _request_json_object(request)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DashboardInputError("File not found.", 404)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DashboardInputError(f"Invalid JSON: {exc}", 400) from exc
    if not isinstance(loaded, dict):
        raise DashboardInputError("File must contain a JSON object.", 400)
    return loaded


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _safe_static_asset_path(root: Path, name: str) -> Path:
    text = str(name or "").strip()
    if not text or text.startswith(".") or "/" in text or "\\" in text or ".." in text:
        raise DashboardInputError("Invalid static asset name.", 400)
    if any(char not in SAFE_JSON_NAME_CHARS for char in text):
        raise DashboardInputError("Invalid static asset name.", 400)
    if Path(text).suffix.lower() not in {".css", ".js", ".png", ".svg", ".ico", ".jpg", ".jpeg", ".webp", ".woff", ".woff2"}:
        raise DashboardInputError("Unsupported static asset type.", 404)
    root_resolved = root.resolve()
    path = (root / text).resolve()
    if path.parent != root_resolved or not path.exists() or not path.is_file():
        raise DashboardInputError("Static asset not found.", 404)
    return path


def _safe_json_path(root: Path, name: str, *, require_exists: bool = True) -> Path:
    text = str(name or "").strip()
    if not text or text.startswith(".") or text.endswith("."):
        raise DashboardInputError("Invalid JSON file name.", 400)
    if "/" in text or "\\" in text or ".." in text:
        raise DashboardInputError("Path traversal is not allowed.", 400)
    if any(char not in SAFE_JSON_NAME_CHARS for char in text):
        raise DashboardInputError("Invalid JSON file name.", 400)
    if not text.endswith(".json"):
        raise DashboardInputError("Only .json files are supported.", 400)
    root_resolved = root.resolve()
    path = (root / text).resolve()
    if path.parent != root_resolved:
        raise DashboardInputError("Path is outside the allowed dashboard directory.", 400)
    if require_exists and not path.exists():
        raise DashboardInputError("File not found.", 404)
    return path


def _safe_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.startswith(".") or text.endswith(".") or ".." in text:
        raise DashboardInputError("Invalid identifier.", 400)
    if any(char not in SAFE_JSON_NAME_CHARS for char in text):
        raise DashboardInputError("Invalid identifier.", 400)
    return text


def _json_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*.json") if path.is_file())


def _query_limit(request: web.Request) -> int:
    try:
        raw = int(request.query.get("limit", "100"))
    except ValueError:
        return 100
    return max(1, min(500, raw))


def _bounded_wait_seconds(value: str | None) -> float:
    try:
        parsed = float(value or "0")
    except ValueError:
        parsed = 0.0
    return max(0.0, min(30.0, parsed))


def _json_response(payload: dict[str, Any], *, status: int = 200) -> web.Response:
    return web.json_response(dashboard_redact(payload), status=status)


def _json_error(message: str, *, status: int) -> web.Response:
    return _json_response({"error": message}, status=status)


def _is_allowed_host_header(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = _hostname_from_netloc(host_header)
    return _is_loopback_host(host)


def _is_allowed_origin(origin: str | None) -> bool:
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return _is_loopback_host(parsed.hostname or "")


def _hostname_from_netloc(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("["):
        return text.split("]", 1)[0] + "]"
    return text.split(":", 1)[0]


def _is_loopback_host(host: str | None) -> bool:
    normalized = str(host or "").strip().lower()
    return normalized in LOOPBACK_HOSTS


def _resolve_bound_port(site: web.TCPSite) -> int | None:
    server = getattr(site, "_server", None)
    sockets = getattr(server, "sockets", None)
    if not sockets:
        return None
    return int(sockets[0].getsockname()[1])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the local Acira dashboard server.")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host to bind.")
    parser.add_argument("--port", type=int, default=8765, help="Local dashboard port.")
    parser.add_argument("--api-key", help="Dashboard API key for UI mutations and runtime ingest.")
    parser.add_argument(
        "--project-dir",
        default=None,
        help="Project root to scope the dashboard to (defaults to the current directory).",
    )
    args = parser.parse_args(argv)
    project_dir = Path(args.project_dir) if args.project_dir else Path.cwd()
    try:
        asyncio.run(run_dashboard(host=args.host, port=args.port, api_key=args.api_key, project_dir=project_dir))
    except OSError as exc:
        if exc.errno in {errno.EADDRINUSE, 10048}:
            print(
                f"Acira dashboard is already running or the port is in use: http://{args.host}:{args.port}/",
                file=sys.stderr,
                flush=True,
            )
            print(
                "Open that URL, stop the existing process, or choose another --port.",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(1) from None
        raise


if __name__ == "__main__":
    main()
