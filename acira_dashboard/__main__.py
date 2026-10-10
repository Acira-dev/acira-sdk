from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _bootstrap_local_venv_for_module() -> None:
    production_dir = Path(__file__).resolve().parents[1]
    local_python = production_dir / ".venv" / "Scripts" / "python.exe"
    if not local_python.exists():
        return

    try:
        current_python = Path(sys.executable).resolve()
        target_python = local_python.resolve()
    except OSError:
        return

    if current_python == target_python:
        return

    completed = subprocess.run([str(target_python), "-m", "acira_dashboard", *sys.argv[1:]], check=False)
    raise SystemExit(completed.returncode)


_bootstrap_local_venv_for_module()

from .server import main  # noqa: E402


if __name__ == "__main__":
    main()
