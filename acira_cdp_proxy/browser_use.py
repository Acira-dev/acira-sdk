"""Compatibility imports for the first-party browser-use adapter.

The production adapter implementation lives at ``acira.adapters.browser_use``.
This module keeps existing imports working without making Acira Core depend on
browser-use from its guard or policy modules.
"""

from acira.adapters import browser_use as _browser_use

globals().update(
    {
        name: value
        for name, value in vars(_browser_use).items()
        if not name.startswith("__")
    }
)

__all__ = [name for name in globals() if not name.startswith("__")]
