from __future__ import annotations

from pathlib import Path
from typing import Any

from .action_gateway import ActionGateway
from .checkpoint import CheckpointStore
from .config import AciraGuardConfig
from .framework_adapters import FrameworkAdapterSpec
from .server import AciraCDPProxy


def protect_cdp(
    cdp_url: str,
    *,
    config: AciraGuardConfig | dict[str, Any] | str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    log_file: str | Path | None = None,
    adapters: tuple[FrameworkAdapterSpec, ...] = (),
) -> AciraCDPProxy:
    """Create a CDP gateway for local, Browserbase, Kernel, or other CDP URLs.

    Caller owns browser lifecycle. Start/stop the returned proxy with
    `await proxy.start()` and `await proxy.stop()`.
    """

    resolved_config = _load_config(config)
    return AciraCDPProxy(
        cdp_url,
        config=resolved_config,
        host=host,
        port=port,
        log_file=log_file,
        adapters=adapters,
    )


def action_gateway(
    *,
    config: AciraGuardConfig | dict[str, Any] | str | Path | None = None,
    checkpoint_store: CheckpointStore | None = None,
    adapters: tuple[FrameworkAdapterSpec, ...] = (),
) -> ActionGateway:
    resolved_config = _load_config(config)
    return ActionGateway(resolved_config, checkpoint_store=checkpoint_store, adapters=adapters)


def _load_config(config: AciraGuardConfig | dict[str, Any] | str | Path | None) -> AciraGuardConfig | dict[str, Any] | None:
    if isinstance(config, (str, Path)):
        return AciraGuardConfig.from_file(config)
    return config
