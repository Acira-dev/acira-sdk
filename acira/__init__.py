"""Acira — the single module a developer imports.

One entry point: ``acira.protect(config=...)``. The framework adapter is declared in the
config file and loaded behind the scenes; developer code never imports an adapter or
references a framework by name.
"""

from .adapters import AciraAdapterConfigurationError
from .sdk import protect
from acira_cdp_proxy.action_gateway import AciraActionBlocked as ActionBlocked

__all__ = [
    "AciraAdapterConfigurationError",
    "ActionBlocked",
    "protect",
]
