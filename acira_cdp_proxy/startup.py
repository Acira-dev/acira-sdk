"""One-time startup summary printed when an Acira-guarded session begins.

Prints which skills are loaded and active. With skills present, skill-based action
classification and interception are on, and the skills are listed. With no skills, Acira
runs in monitoring-only mode — actions are observed/recorded and manual HITL + dashboard
remain available, but skill-based interception is off. No skills is a valid configuration:
this never warns or errors.
"""

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


def format_skill_activation_summary(config: AciraGuardConfig, *, framework: str | None = None) -> list[str]:
    """Build the human-readable startup summary lines."""
    general = list(getattr(config, "general_skills", ()) or ())
    specific = list(getattr(config, "specific_skills", ()) or ())
    who = f"{framework} adapter" if framework else "adapter"

    if not general and not specific:
        return [
            f"{_PREFIX} {who} active: no general or captured policies loaded.",
            f"{_PREFIX} {len(config.rules)} explicit rules; supported adapter boundaries still apply. "
            "Add policies with acira policy add or acira policy import.",
        ]

    lines = [
        f"{_PREFIX} {who} active - skill-based action classification and interception ON.",
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
) -> None:
    """Print the startup summary once. Best-effort: never raises into the caller."""
    out = stream if stream is not None else sys.stderr
    try:
        for line in format_skill_activation_summary(config, framework=framework):
            print(line, file=out, flush=True)
    except Exception:
        # A summary print must never break session startup.
        pass
