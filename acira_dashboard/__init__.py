"""Local Acira dashboard server."""

from __future__ import annotations

from typing import Any

__all__ = [
    "DashboardApiCheckpointStore",
    "DashboardApiClient",
    "DashboardApiConfig",
    "DashboardApiReviewBackend",
    "DashboardApiSessionReporter",
    "DashboardCheckpointStore",
    "DashboardEventStore",
    "DashboardPaths",
    "DashboardReviewBackend",
    "DashboardRuntimeControl",
    "DashboardRuntimeStore",
    "create_app",
    "parse_dashboard_api_config",
    "run_dashboard",
]


def __getattr__(name: str) -> Any:
    if name in {"DashboardPaths", "create_app", "run_dashboard"}:
        from .server import DashboardPaths, create_app, run_dashboard

        return {"DashboardPaths": DashboardPaths, "create_app": create_app, "run_dashboard": run_dashboard}[name]
    if name in {"DashboardCheckpointStore", "DashboardEventStore"}:
        from .store import DashboardCheckpointStore, DashboardEventStore

        return {"DashboardCheckpointStore": DashboardCheckpointStore, "DashboardEventStore": DashboardEventStore}[name]
    if name in {"DashboardReviewBackend", "DashboardRuntimeControl", "DashboardRuntimeStore"}:
        from .runtime import DashboardReviewBackend, DashboardRuntimeControl, DashboardRuntimeStore

        return {
            "DashboardReviewBackend": DashboardReviewBackend,
            "DashboardRuntimeControl": DashboardRuntimeControl,
            "DashboardRuntimeStore": DashboardRuntimeStore,
        }[name]
    if name in {
        "DashboardApiCheckpointStore",
        "DashboardApiClient",
        "DashboardApiConfig",
        "DashboardApiReviewBackend",
        "DashboardApiSessionReporter",
        "parse_dashboard_api_config",
    }:
        from .api_client import (
            DashboardApiCheckpointStore,
            DashboardApiClient,
            DashboardApiConfig,
            DashboardApiReviewBackend,
            DashboardApiSessionReporter,
            parse_dashboard_api_config,
        )

        return {
            "DashboardApiCheckpointStore": DashboardApiCheckpointStore,
            "DashboardApiClient": DashboardApiClient,
            "DashboardApiConfig": DashboardApiConfig,
            "DashboardApiReviewBackend": DashboardApiReviewBackend,
            "DashboardApiSessionReporter": DashboardApiSessionReporter,
            "parse_dashboard_api_config": parse_dashboard_api_config,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
