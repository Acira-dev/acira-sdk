from __future__ import annotations

import argparse
import asyncio
import difflib
import importlib.metadata
import json
import sys
from pathlib import Path

from .journal import Journal
from .local import Registry
from .project import CATALOG, NAME, PolicyConflict, Project, catalog_policy, digest, initialize, read_json, validate_policy


def output(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="acira", description="Local policies, human review, and action history for your existing browser agent.")
    cli.add_argument("--version", action="version", version="Acira " + importlib.metadata.version("acira"))
    commands = cli.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Connect your existing project: configure policies and show SDK integration steps")
    init.add_argument("--project", default=".")
    init.add_argument("--agent", default="default")
    init.add_argument("--adapter", type=normalize_adapter, choices=["playwright", "browser_use"])
    init.add_argument("--general")
    init.add_argument("--specific")
    init.add_argument("--policies", help="Comma-separated bundled policy names, or none")
    init.add_argument("--no-dashboard", action="store_true")
    init.add_argument("--yes", action="store_true", help="Use defaults without interactive questions")
    demo = commands.add_parser("demo", help="Optional separate test drive; not required to integrate Acira")
    demo.add_argument("--directory", help="Separate demo project folder; defaults to Acira's private user state")
    demo.add_argument("--port", type=int, default=0, help="Test-drive port; by default choose an available port")
    demo.add_argument("--no-open", action="store_true", help="Print the test-drive address without opening browser tabs")
    demo.add_argument("--adapter", type=lambda value: normalize_adapter(value).replace("_", "-"), choices=["playwright", "browser-use"], default="playwright", help="Choose scripted Playwright or a local-model browser-use agent")
    demo.add_argument("--public-sites", action="store_true", help="Try read-only real documentation websites with Playwright")
    demo.add_argument("--model", default="qwen3:4b-instruct", help="Downloaded local Ollama model for browser-use")
    demo.add_argument("--ollama-host", default="http://127.0.0.1:11434", help="Loopback Ollama endpoint for browser-use")
    opened = commands.add_parser("open", help="Open the shared local dashboard")
    opened.add_argument("--no-browser", action="store_true")
    for name in ("status", "doctor", "sync"):
        sub = commands.add_parser(name, help={"status": "Inspect project and agent state", "doctor": "Check setup without launching browsers or changing policies", "sync": "Rescan policies and report synchronization state"}[name])
        sub.add_argument("--project")
        sub.add_argument("--agent", default="default")
        if name == "doctor":
            sub.add_argument("--json", action="store_true", help="Machine-readable checks, versions and repair steps")
            sub.add_argument("--skip", choices=["chromium"], action="append", default=[], help="Skip the bundled Chromium check when your application uses an explicit custom browser; this does not validate it")
            sub.add_argument("--recover", action="store_true", help="Restore the previous complete snapshot after an interrupted policy write")
    extension = commands.add_parser("extension", help="Install the Chrome capture extension from this SDK").add_subparsers(dest="operation", required=True)
    install = extension.add_parser("install", help="Export the bundled extension and show Chrome setup steps")
    install.add_argument("--directory", help="New output folder; defaults to Acira's private user state")
    install.add_argument("--replace", action="store_true", help="Update an existing Acira extension installation")
    policies = commands.add_parser("policy", help="Select, import and configure general or specific policies in the terminal").add_subparsers(dest="operation", required=True)
    for name in ("list", "add", "import", "configure", "enable", "disable", "validate", "update"):
        sub = policies.add_parser(name)
        sub.add_argument("--project")
        sub.add_argument("--agent", default="default")
        if name in {"add", "configure", "enable", "disable"}:
            sub.add_argument("source", nargs="?", help="Policy name/ID; omit for interactive selection")
        if name == "import":
            sub.add_argument("source")
        if name == "list":
            sub.add_argument("--json", action="store_true", help="Machine-readable policy documents and locations")
        if name == "configure":
            sub.add_argument("--decision", choices=["ask", "block", "allow"])
            sub.add_argument("--risk", help="General-policy risk ID")
            sub.add_argument("--target", help="Specific-policy captured target ID")
        if name == "add":
            sub.add_argument("--catalog", help="Explicit HTTPS URL of an acira.catalog.v1 manifest")
        if name == "import":
            sub.add_argument("--replace", action="store_true")
        if name == "update":
            sub.add_argument("--accept", action="store_true", help="Accept the displayed bundled-policy updates")
    server = commands.add_parser("server").add_subparsers(dest="operation", required=True)
    for name in ("status", "stop", "serve"):
        server.add_parser(name)
    return cli


def normalize_adapter(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def prompt_choice(label: str, default: str, valid) -> str:
    while True:
        value = prompt(label, default)
        try:
            valid(value)
            return value
        except ValueError as exc:
            print(f"Acira: {exc}. Try again.", file=sys.stderr)


def prompt(label: str, default: str) -> str:
    answer = input(f"{label} [{default}]: ").strip()
    return answer or default


def run(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            root = Path(args.project).expanduser().resolve()
            if not NAME.fullmatch(args.agent):
                raise ValueError("Agent names use 1–80 letters, numbers, dots, underscores or hyphens and start with a letter or number")
            existing = Project(root)
            if existing.path.is_file() and args.agent in existing.document()["agents"]:
                existing.snapshot(args.agent)
                Registry().register(existing.root, existing.document()["name"])
                project = existing
                print(f"Already initialized: {root.name} / {args.agent}. Existing settings were kept. Use policy commands or the dashboard to edit them.")
            else:
                interactive = sys.stdin.isatty() and not args.yes
                detected = "playwright"
                for name in ("requirements.txt", "pyproject.toml"):
                    path = root / name
                    if path.is_file() and "browser-use" in path.read_text(encoding="utf-8"):
                        detected = "browser_use"
                def valid_adapter(value):
                    if normalize_adapter(value) not in {"playwright", "browser_use"}:
                        raise ValueError("Choose playwright or browser-use")
                adapter = normalize_adapter(args.adapter or (prompt_choice("Integration: playwright or browser-use", detected, valid_adapter) if interactive else detected))
                base = "policies" if args.agent == "default" else f"policies/{args.agent}"
                general = args.general or (prompt("General policy folder (existing folders are supported)", base + "/general") if interactive else base + "/general")
                def valid_specific(value):
                    if (root / value).resolve() == (root / general).resolve():
                        raise ValueError("General and specific policy folders must be different")
                specific = args.specific or (prompt_choice("Captured policy folder", base + "/specific", valid_specific) if interactive else base + "/specific")
                print("Starter choices: payment submissions ask after a verified guard.submit() contract; credentials ask; destructive actions block; downloads ask. Page presence alone does not ask.")
                def valid_policies(value):
                    if value.strip().lower() != "none":
                        for name in value.split(","):
                            catalog_policy(name.strip())
                selected = args.policies or (prompt_choice("Policies: payment,credentials,destructive-actions,downloads; or none", ",".join(CATALOG), valid_policies) if interactive else ",".join(CATALOG))
                connect = not args.no_dashboard
                if interactive and not args.no_dashboard:
                    def valid_connection(value):
                        if value.lower() not in {"y", "yes", "n", "no"}:
                            raise ValueError("Choose yes or no")
                    connect = prompt_choice("Automatically connect to your trusted local dashboard? yes/no", "yes", valid_connection).lower() in {"y", "yes"}
                names = [] if selected.strip().lower() == "none" else [name.strip() for name in selected.split(",")]
                project = initialize(root, agent=args.agent, adapter=adapter, general=general, specific=specific, selected=names, dashboard=connect)
                if not names:
                    print("No starter policies selected. Add one later with acira policy add or acira policy import.")
            # Initialization is idempotent: guidance must describe the persisted
            # agent, including when an existing profile differs from the flags.
            profile = project.snapshot(args.agent).profile
            print(f"Configured: {project.path}\n\n1. Add Acira to your existing async entry point ({profile['adapter']}):")
            print("\nfrom acira import protect")
            if profile["adapter"] == "playwright":
                print("from playwright.async_api import async_playwright")
            arguments = "" if args.agent == "default" else f"agent={args.agent!r}"
            print(f"\nasync with protect({arguments}) as guard:")
            if profile["adapter"] == "playwright":
                print("    async with async_playwright() as p:\n        guard.patch_playwright(p)\n        # Keep your Chromium launch/connect and existing workflow here.\n        ...")
            else:
                print("    # Create BrowserSession and Agent here using your existing\n    # browser settings and model, then await agent.run().\n    ...")
            print(f"\n2. In a terminal opened at {project.root}, verify configuration:")
            print(f"acira doctor --agent {args.agent}\nacira policy validate --agent {args.agent}")
            print("\n3. Start your agent using its usual command. Protection begins when this code runs.")
            print("\nManage policies entirely from the terminal:\n  acira policy add          # select a general protection\n  acira extension install   # set up Chrome capture for specific targets\n  acira policy import capture.json\n  acira policy configure    # select a policy and change its decision\n  acira policy list")
            print("\n4. Optional dashboard (now or later): acira open\nInspect Agents, Approvals and Activity for your project's real run.")
            if not project.document().get("dashboard", {}).get("connect", False):
                print("This project has automatic dashboard connection disabled. Use protect(..., dashboard=True) to connect a run.")
            print("\nYour existing browser/model can be reused. Setup does not launch a browser, download models, or run a demo.\nOptional separate test drive: acira demo")
            return
        if args.command == "demo":
            if not 0 <= args.port <= 65535:
                raise ValueError("Demo port must be between 0 and 65535")
            if args.public_sites and args.adapter != "playwright":
                raise ValueError("The public-websites test drive uses Playwright. Run acira demo --public-sites; the local browser-use demo remains available separately.")
            try:
                if args.public_sites:
                    from .public_demo import main as demo_main
                else:
                    from .demo import main as demo_main
                if args.adapter == "browser-use":
                    from importlib.util import find_spec
                    if find_spec("browser_use") is None:
                        raise ValueError('Install the demo integrations: python -m pip install "acira[browser-use,playwright]"; then install Chromium and a local Ollama model')
            except ModuleNotFoundError as exc:
                if exc.name == "playwright" or exc.name.startswith("playwright."):
                    extras = "browser-use,playwright" if args.adapter == "browser-use" else "playwright"
                    label = "Playwright integration" if args.adapter == "playwright" else "browser-use demo integrations"
                    raise ValueError('Install the ' + label + ': python -m pip install "acira[' + extras + ']"; then python -m playwright install chromium') from None
                raise
            try:
                asyncio.run(demo_main(auto_open=not args.no_open, port=args.port, directory=args.directory,
                    adapter=args.adapter.replace("-", "_"), model=args.model, ollama_host=args.ollama_host))
            except KeyboardInterrupt:
                pass
            return
        if args.command == "open":
            from .service import open_dashboard
            url = open_dashboard(launch_browser=not args.no_browser)
            print(url if args.no_browser else "Acira opened in your browser. Closing the tab does not stop enforcement.")
            return
        if args.command == "extension":
            from .extension import install_extension
            directory = install_extension(args.directory, replace=args.replace)
            print(f"Acira Capture is ready in:\n{directory}\n\n1. Open chrome://extensions in Chrome (or edge://extensions in Edge).\n2. Turn on Developer mode, choose Load unpacked, and select that folder.\n3. On a website, click Acira Capture, select a target, and choose Ask first or Block.\n4. Download the JSON, then run: acira policy import <downloaded-file.json>\n5. Configure it without the dashboard: acira policy configure\n\nOptional direct saving: pair the extension from acira open → Settings.\nThe SDK does not enable extensions or change browser permissions for you.")
            return
        if args.command == "server":
            from .service import admin_request, serve, verified_descriptor
            if args.operation == "serve":
                asyncio.run(serve())
                return
            found = verified_descriptor()
            if args.operation == "stop" and found:
                output(admin_request(found, "/api/stop"))
            else:
                output({"running": bool(found), "url": found["url"] if found else None})
            return
        project = Project.find(args.project)
        if args.command == "doctor" and args.recover:
            from .revisions import recover
            from .local import file_lock
            with file_lock(project.root / ".acira/policy.lock"):
                recover(project)
        if args.command == "doctor":
            from .diagnostics import doctor, print_doctor
            result = doctor(project, args.agent, skip_chromium="chromium" in args.skip)
            output(result) if args.json or not sys.stdout.isatty() else print_doctor(result)
            if not result["healthy"]:
                raise SystemExit(1)
            return
        snapshot = project.snapshot(args.agent)
        if args.command in {"status", "doctor", "sync"}:
            from .service import verified_descriptor
            found = verified_descriptor()
            storage = None
            if args.command == "sync":
                from .retention import maintain
                storage = maintain(project)
            result = {"project": str(project.root), "agent": args.agent, "revision": snapshot.revision,
                      "policy_count": len(snapshot.documents), "dashboard": found["url"] if found else "not running",
                      "runs": Journal(project.root).runs()}
            if storage is not None:
                result["storage"] = storage
            project.remember_revision(args.agent, snapshot)
            output(result)
            return
        if args.operation == "list":
            if args.json:
                output({"catalog": list(CATALOG), "policies": project.policies(args.agent)})
            else:
                from .policy_cli import show_policies
                show_policies(project, args.agent)
        elif args.operation == "configure":
            from .policy_cli import configure_policy
            result = configure_policy(project, args.agent, args.source, decision=args.decision, risk=args.risk, target=args.target)
            print("Saved · " + Path(result["path"]).name + " · affected agents: " + ", ".join(result["affected_agents"]))
            print("Running agents apply it at their next guarded action. Use acira status to inspect the active revision.")
        elif args.operation in {"enable", "disable"}:
            from .policy_cli import set_enabled
            result = set_enabled(project, args.agent, args.source, enabled=args.operation == "enable")
            output(result)
        elif args.operation == "validate":
            output({"valid": True, "revision": snapshot.revision, "policy_count": len(snapshot.documents)})
        elif args.operation in {"add", "import"}:
            from .catalog import policy as remote_policy
            remote = getattr(args, "catalog", None)
            if args.operation == "add" and args.source is None:
                if remote:
                    raise ValueError("Provide the policy name when using --catalog")
                from .policy_cli import select_bundled
                args.source = select_bundled()
            value = remote_policy(remote, args.source) if remote else catalog_policy(args.source) if args.operation == "add" else read_json(Path(args.source))
            try:
                kind, policy_id = validate_policy(value)
            except (ValueError, TypeError) as exc:
                origin = args.source if args.operation == "import" else "Policy " + args.source
                raise ValueError(f"{origin}: {exc}") from None
            filename = (CATALOG.get(args.source, args.source.removeprefix("acira.general.")) if args.operation == "add" and not remote else "import-" + digest({"id": policy_id})[:16]) + ".json"
            for existing in project.policies(args.agent):
                if existing["id"] == policy_id:
                    filename = Path(existing["path"]).name
                    break
            path = project.folders(args.agent)[kind] / filename
            if path.exists() and read_json(path) == value:
                output({"unchanged": True, "path": str(path)})
                return
            expected = digest(read_json(path)) if path.exists() and getattr(args, "replace", False) else None
            try:
                saved = project.save_policy(args.agent, filename, value, expected=expected)
            except PolicyConflict:
                raise ValueError("An installed policy with this ID differs. Compare the files, then rerun acira policy import <file> --replace to replace it explicitly.") from None
            output(saved)
        elif args.operation == "update":
            updates = []
            from .catalog import updated
            for entry in project.policies(args.agent):
                if entry["document"].get("acira_source"):
                    old, new = entry["document"], updated(entry["document"])
                    if old != new:
                        updates.append((Path(entry["path"]), old, new))
            for name, stem in CATALOG.items():
                path = project.folders(args.agent)["general"] / (stem + ".json")
                if not path.exists():
                    continue
                old, new = read_json(path), catalog_policy(name)
                if old.get("acira_source"):
                    continue
                new["acira_decisions"] = old.get("acira_decisions", new["acira_decisions"])
                if old == new:
                    continue
                updates.append((path, old, new))
            for path, old, new in updates:
                print("".join(difflib.unified_diff(json.dumps(old, indent=2).splitlines(True), json.dumps(new, indent=2).splitlines(True), fromfile=path.name, tofile=path.name + " (proposed)")))
            if args.accept:
                for path, old, new in updates:
                    project.save_policy(args.agent, path.name, new, expected=digest(old))
            output({"updates": len(updates), "applied": bool(args.accept), "note": "Review diffs, then use --accept. Project-specific decisions are preserved."})
    except (KeyboardInterrupt, EOFError):
        print("Acira: Cancelled. No further changes were made.", file=sys.stderr)
        raise SystemExit(130) from None
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        print(f"Acira: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
