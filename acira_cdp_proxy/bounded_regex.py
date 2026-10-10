"""Python-compatible configured expressions with finite matching time."""
from functools import lru_cache
import re
import time

import regex

MAX_PATTERNS = 32
MAX_PATTERN_LENGTH = 2048
MATCH_BUDGET = 0.05


@lru_cache(maxsize=128)
def compile_pattern(pattern: str):
    if not isinstance(pattern, str) or len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError("Configured regex patterns must contain at most 2048 characters")
    # Preserve the accepted Python syntax; do not silently enable recursive or
    # fuzzy extensions in imported legacy configurations.
    try:
        re.compile(pattern)
        return regex.compile(pattern, regex.VERSION0, cache_pattern=False)
    except (re.error, regex.error, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid configured regex: "+str(exc)) from exc


def remaining(deadline: float) -> float:
    budget = deadline-time.monotonic()
    if budget <= 0:
        raise TimeoutError("Configured expression budget exhausted")
    return budget
