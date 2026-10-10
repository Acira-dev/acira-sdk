from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from .config import AciraGuardConfig
from .logger import CDPLogger
from .runner import run_supervised_agent
from .server import AciraCDPProxy


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        return parse_run_args(argv[1:])

    parser = argparse.ArgumentParser(description="Dry-run the local Acira CDP proxy.")
    parser.add_argument("--cdp-url", required=True, help="Existing Chrome CDP URL, for example http://127.0.0.1:9222")
    parser.add_argument("--host", default="127.0.0.1", help="Local proxy host")
    parser.add_argument("--port", type=int, default=0, help="Local proxy port, or 0 for any free port")
    parser.add_argument("--config", help="JSON or YAML Acira config file")
    parser.add_argument("--log-file", help="Optional redacted CDP WebSocket log file")
    parser.add_argument("--serve", action="store_true", help="Keep the proxy running after printing /json/version")
    args = parser.parse_args(argv)
    args.command = "preview"
    return args


def parse_run_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="acira run",
        description="Run an existing CDP agent through Acira without taking ownership of Chrome.",
    )
    parser.add_argument("--cdp-url", required=True, help="Existing browser CDP URL, for example http://127.0.0.1:9222")
    parser.add_argument("--host", default="127.0.0.1", help="Local proxy host")
    parser.add_argument("--port", type=int, default=0, help="Local proxy port, or 0 for any free port")
    parser.add_argument("--config", help="JSON or YAML Acira config file")
    parser.add_argument("--log-file", help="Optional redacted CDP WebSocket log file")
    parser.add_argument(
        "--suspend-child-during-review",
        action="store_true",
        help="Best-effort pause/resume of the supervised agent subprocess during HITL review.",
    )
    parser.add_argument("agent_command", nargs=argparse.REMAINDER, help="Agent command to launch after '--'")
    args = parser.parse_args(argv)
    if not args.agent_command or args.agent_command == ["--"]:
        parser.error("missing agent command after '--'")
    args.command = "run"
    return args


async def preview_proxy(args: argparse.Namespace) -> None:
    config = AciraGuardConfig.from_file(args.config) if args.config else None
    server = AciraCDPProxy(
        args.cdp_url,
        config=config,
        host=args.host,
        port=args.port,
        logger=CDPLogger(Path(args.log_file)) if args.log_file else None,
    )
    await server.start()

    try:
        version_url = f"{server.local_http_url}/json/version"
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(version_url)
            response.raise_for_status()
            payload = response.json()

        print(f"Proxy listening: {server.local_http_url}")
        print(f"Upstream CDP: {args.cdp_url}")
        if args.config:
            print(f"Acira config: {args.config}")
        print("Rewritten /json/version:")
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        print(f"CDP log file: {server.logger.path or 'disabled'}")

        if args.serve:
            print("Serving until interrupted.")
            await asyncio.Future()
    finally:
        await server.stop()


def main() -> None:
    args = parse_args()
    try:
        if args.command == "run":
            exit_code = asyncio.run(
                run_supervised_agent(
                    cdp_url=args.cdp_url,
                    config_path=args.config,
                    host=args.host,
                    port=args.port,
                    log_file=args.log_file,
                    agent_command=args.agent_command,
                    suspend_child_during_review=args.suspend_child_during_review,
                )
            )
            raise SystemExit(exit_code)
        asyncio.run(preview_proxy(args))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
