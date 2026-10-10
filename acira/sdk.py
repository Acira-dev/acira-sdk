from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from acira_cdp_proxy.checkpoint import CheckpointStore
from acira_cdp_proxy.config import AciraGuardConfig

from .adapters import (
    AciraAdapterConfigurationError,
    discover_adapter_ids,
    load_adapter,
)


def protect(
    config: AciraGuardConfig | dict[str, Any] | str | Path | None = None,
    *,
    adapter: str | None = None,
    project: str | Path | None = None,
    agent: str = "default",
    dashboard: str | bool = "auto",
    log_file: str | Path | None = None,
    checkpoint_store: CheckpointStore | None = None,
    **adapter_options: Any,
) -> Any:
    """Single, adapter-agnostic entry point for protecting an agent session.

    The framework adapter (Playwright, browser-use, ...) is declared in the config file
    under ``adapters`` and loaded dynamically — the developer never imports or names an
    adapter. Returns the adapter's ``guard`` async context manager, unchanged.
    """
    if isinstance(config, (str, Path)) and Path(config).suffix.lower() == ".json":
        document = _load_raw_config_file(Path(config))
        if document.get("schema") == "acira.project.v1":
            if project is not None:
                raise ValueError("Choose one project/configuration location")
            project, config = Path(config).resolve().parent, None
    if project is not None or config is None:
        from .project import Project
        from .runtime import ProjectContext, ProjectRuntime
        selected = Project.find(project)
        if config is not None:
            raise ValueError("Use project= with its agent configuration, or legacy config=, not both")
        if checkpoint_store is not None or adapter_options.get("runtime_control") is not None:
            raise ValueError("Project mode owns durable checkpoints and runtime control; use config= for custom runtime hooks")
        runtime = ProjectRuntime(selected, agent, dashboard=dashboard, review_backend=adapter_options.pop("review_backend", None))
        adapter_id = _select_adapter(runtime.snapshot.config, explicit=adapter, registered_ids=discover_adapter_ids())
        instance = load_adapter(adapter_id, config=runtime.snapshot.config, log_file=log_file,
                                checkpoint_store=runtime, module_path=None,
                                options={**adapter_options, "review_backend": runtime, "runtime_control": runtime})
        return ProjectContext(instance, runtime)
    resolved_config, raw_config = _load_config_bundle(config)
    if dashboard is False and raw_config is not None:
        raw_config = {**raw_config, "dashboard": {"enabled": False}}
    available = discover_adapter_ids() | _adapters_declaring_module(resolved_config)
    adapter_id = _select_adapter(resolved_config, explicit=adapter, registered_ids=available)
    checkpoint_store, adapter_options = _apply_configured_dashboard_runtime(
        resolved_config,
        raw_config,
        checkpoint_store=checkpoint_store,
        adapter_options=adapter_options,
        framework=adapter_id,
    )
    return load_adapter(
        adapter_id,
        config=resolved_config,
        log_file=log_file,
        checkpoint_store=checkpoint_store,
        module_path=_adapter_module_path(resolved_config, adapter_id),
        options=adapter_options,
    )


def _adapter_module_path(config: AciraGuardConfig, adapter_id: str) -> str | None:
    """An explicit ``module``/``entry`` dotted path for an externally-shipped adapter."""
    adapter_cfg = config.adapters.get(adapter_id)
    raw = getattr(adapter_cfg, "raw", None)
    if isinstance(raw, Mapping):
        module_value = raw.get("module") or raw.get("entry")
        if isinstance(module_value, str) and module_value.strip():
            return module_value.strip()
    return None


def _adapters_declaring_module(config: AciraGuardConfig) -> set[str]:
    """Adapter ids that point at an external module — known even if not under acira.adapters."""
    declared: set[str] = set()
    for adapter_id, adapter_cfg in config.adapters.items():
        raw = getattr(adapter_cfg, "raw", None)
        if isinstance(raw, Mapping) and str(raw.get("module") or raw.get("entry") or "").strip():
            declared.add(adapter_id)
    return declared


def _load_config(config: AciraGuardConfig | dict[str, Any] | str | Path | None) -> AciraGuardConfig:
    return _load_config_bundle(config)[0]


def _load_config_bundle(
    config: AciraGuardConfig | dict[str, Any] | str | Path | None,
) -> tuple[AciraGuardConfig, dict[str, Any] | None]:
    if isinstance(config, AciraGuardConfig):
        return config, None
    if isinstance(config, (str, Path)):
        file_path = Path(config)
        raw_config = _load_raw_config_file(file_path)
        return AciraGuardConfig.from_dict(raw_config, base_path=file_path.parent, source=str(file_path)), raw_config
    if config is None:
        return AciraGuardConfig.default_config(), None
    return AciraGuardConfig.from_dict(config), config


def _load_raw_config_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise RuntimeError("YAML Acira config files require PyYAML to be installed") from exc
        loaded = yaml.safe_load(text)
    else:
        loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("Acira config file must contain a JSON/YAML object")
    return loaded


def _apply_configured_dashboard_runtime(
    resolved_config: AciraGuardConfig,
    raw_config: dict[str, Any] | None,
    *,
    checkpoint_store: CheckpointStore | None,
    adapter_options: dict[str, Any],
    framework: str = "agent",
) -> tuple[CheckpointStore | None, dict[str, Any]]:
    raw_dashboard = raw_config.get("dashboard") if isinstance(raw_config, dict) else None
    if raw_dashboard is None:
        return checkpoint_store, adapter_options
    if not isinstance(raw_dashboard, dict):
        _report_adapter_error("dashboard must be an object")
    # The agent opts in with a boolean; it never specifies a URL or token.
    if not bool(raw_dashboard.get("connect", raw_dashboard.get("enabled", False))):
        return checkpoint_store, adapter_options

    from acira_dashboard.api_client import (
        DashboardApiCheckpointStore,
        DashboardApiClient,
        DashboardApiReviewBackend,
        DashboardApiSessionReporter,
        parse_dashboard_api_config,
    )

    project_dir = Path.cwd()
    try:
        dashboard_config = parse_dashboard_api_config(raw_config, project_dir=project_dir)
    except ValueError as exc:
        _report_adapter_error(str(exc))

    if not dashboard_config.enabled:
        # Opted in, but no local dashboard was discovered. Run without one — silent
        # fallback. Terminal HITL (when terminal_review is enabled) still applies via the
        # guard's own backend, so human approval is never lost just because the dashboard
        # is down.
        return checkpoint_store, adapter_options

    conflicting = []
    if checkpoint_store is not None:
        conflicting.append("checkpoint_store")
    for option_name in ("review_backend", "runtime_control"):
        if adapter_options.get(option_name) is not None:
            conflicting.append(option_name)
    if conflicting:
        joined = ", ".join(conflicting)
        _report_adapter_error(f"dashboard connect=true cannot be combined with manual dashboard/runtime hooks: {joined}")

    dashboard_client = DashboardApiClient(dashboard_config)
    configured_options = dict(adapter_options)
    # HITL is dual-channel: the review backend always races a terminal prompt against the
    # dashboard review and takes whichever responds first; on timeout it applies the
    # policy default decision.
    configured_options["review_backend"] = DashboardApiReviewBackend(
        dashboard_client,
        timeout_seconds=dashboard_config.review_timeout_seconds,
        terminal_review_config=resolved_config,
        default_decision=resolved_config.default_review_decision,
    )
    # Fire-and-forget session lifecycle (register once, end once); no liveness polling.
    configured_options["runtime_control"] = DashboardApiSessionReporter(
        dashboard_client,
        framework=framework,
        agent_name=dashboard_config.agent_name or (resolved_config.agent_name or ""),
        project_root=str(project_dir),
    )
    return DashboardApiCheckpointStore(dashboard_client), configured_options


def _select_adapter(
    config: AciraGuardConfig,
    *,
    explicit: str | None,
    registered_ids: set[str],
) -> str:
    configured = {
        adapter_id: adapter_config
        for adapter_id, adapter_config in config.adapters.items()
        if adapter_config.enabled
    }
    unknown = sorted(set(configured) - registered_ids)
    if unknown:
        _report_adapter_error(f"Unknown Acira adapter configured: {', '.join(unknown)}")
    if explicit is not None:
        normalized = explicit.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized not in configured:
            _report_adapter_error(f"Acira adapter is not enabled in config: {normalized}")
        if normalized not in registered_ids:
            _report_adapter_error(f"Unknown Acira adapter configured: {normalized}")
        return normalized
    if not configured:
        _report_adapter_error("No Acira adapter configured. Add an enabled adapter under config.adapters.")
    if len(configured) > 1:
        enabled = ", ".join(sorted(configured))
        _report_adapter_error(f"Multiple Acira adapters are enabled ({enabled}); pass adapter=... explicitly.")
    return next(iter(configured))


def _report_adapter_error(message: str) -> None:
    print(f"[Acira SDK] {message}", file=sys.stderr, flush=True)
    raise AciraAdapterConfigurationError(message)
