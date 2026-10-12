"""Best-effort policy loading and attachment diagnostics; never a protection proof."""

from __future__ import annotations

import sys
from typing import Any

from .config import AciraGuardConfig

_PREFIX = "[Acira]"


def _skill_label(skill: Any) -> str:
    for attr in ("name", "id"):
        value = getattr(skill, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return skill.__class__.__name__


def format_skill_activation_summary(config: AciraGuardConfig, *, framework: str | None = None, attached: bool = True) -> list[str]:
    """Build the human-readable startup summary lines."""
    general = list(getattr(config, "general_skills", ()) or ())
    specific = list(getattr(config, "specific_skills", ()) or ())
    who = f"{framework} adapter" if framework else "adapter"

    status = "active" if attached else "policies loaded; waiting for guard.patch_playwright(p) before Chromium launch/connect, or guard.patch_page(page) for an existing context"
    if not general and not specific:
        return [
            f"{_PREFIX} {who} {status}: no general or captured policies loaded.",
            f"{_PREFIX} {len(config.rules)} explicit rules; supported adapter boundaries still apply. "
            "Add policies with acira policy add or acira policy import.",
        ]

    lines = [
        f"{_PREFIX} {who} {status}.",
    ]
    if general:
        lines.append(f"{_PREFIX}   general skills ({len(general)}): {', '.join(_skill_label(s) for s in general)}")
    if specific:
        lines.append(f"{_PREFIX}   specific skills ({len(specific)}): {', '.join(_skill_label(s) for s in specific)}")
    return lines


def print_skill_activation_summary(
    config: AciraGuardConfig,
    *,
    framework: str | None = None,
    stream: Any = None,
    attached: bool = True,
) -> None:
    """Print the startup summary once. Best-effort: never raises into the caller."""
    if not config.terminal_log:
        return
    out = stream if stream is not None else sys.stderr
    try:
        for line in format_skill_activation_summary(config, framework=framework, attached=attached):
            print(line, file=out, flush=True)
    except Exception:
        # A summary print must never break session startup.
        pass


def print_attachment_status(message: str, *, enabled: bool = True) -> None:
    if not enabled:
        return
    try:
        print(f"{_PREFIX} {message}", file=sys.stderr, flush=True)
    except Exception:
        pass
