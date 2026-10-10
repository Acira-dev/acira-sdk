"""Acira CLI. Use init/open for project mode; dashboard/run retain legacy aliases."""

from __future__ import annotations

import sys

def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        from .cli import parser
        parser().print_help()
        return
    command, rest = args[0], args[1:]
    # "run" is kept as a back-compat alias for "dashboard".
    if command in ("dashboard", "run"):
        from acira_dashboard.server import main as dashboard_main

        dashboard_main(rest)
        return
    from .cli import run
    run(args)


if __name__ == "__main__":
    main()
