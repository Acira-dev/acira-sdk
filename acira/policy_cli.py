"""Readable terminal policy selection; policy writes use the shared validator."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

from .project import CATALOG, catalog_policy, digest


def choose(label, items):
    if not sys.stdin.isatty():
        raise ValueError(f"{label}: provide the name/ID explicitly when using a noninteractive terminal")
    for index, (key, description) in enumerate(items, 1):
        print(f"  {index}. {description} ({key})")
    answer = input(label + ": ").strip()
    if answer.isdecimal() and 1 <= int(answer) <= len(items):
        return items[int(answer)-1][0]
    if answer in {key for key, _ in items}:
        return answer
    raise ValueError("Choose a listed number or exact ID. No files were changed.")


def select_bundled():
    return choose("Choose a general protection", [(name, catalog_policy(name).get("name", name)) for name in CATALOG])


def find_policy(project, agent, name):
    entries = project.policies(agent)
    if not entries:
        raise ValueError("No policies installed. Use acira policy add or acira policy import capture.json first.")
    if not name:
        name = choose("Choose a policy", [(entry["id"], entry["name"]+" · "+entry["kind"]) for entry in entries])
    stem = CATALOG.get(name, name)
    matches = [entry for entry in entries if name in {entry["id"], entry["name"], Path(entry["path"]).name}
               or Path(entry["path"]).stem == stem]
    if len(matches) != 1:
        raise ValueError("Policy not found or name is ambiguous. Use the exact ID from acira policy list.")
    return matches[0]


def show_policies(project, agent):
    profile = project.snapshot(agent).profile
    entries = project.policies(agent)
    print(f"{project.root.name} / {agent}")
    for kind, label in (("general", "General protections"), ("specific", "Specific policies · captured website targets")):
        print("\n"+label)
        group = [entry for entry in entries if entry["kind"] == kind]
        if not group:
            print("  None installed")
        for entry in group:
            status = "disabled" if entry["id"] in profile.get("disabled_policies", []) else "enabled"
            print(f"  {entry['name']} · {status}\n    ID: {entry['id']} · {Path(entry['path']).name}")
    print("\nGeneral: acira policy add\nSpecific: acira extension install → capture → acira policy import capture.json")
    print("Configure: acira policy configure <ID>\nMachine-readable output: acira policy list --json")


def configure_policy(project, agent, name, *, decision=None, risk=None, target=None):
    entry = find_policy(project, agent, name)
    value = copy.deepcopy(entry["document"])
    if entry["kind"] == "specific":
        if risk:
            raise ValueError("Specific policies use --target, not --risk")
        items = value["targets"]
        options = [(str(item["id"]), (item.get("fingerprint") or item.get("match") or {}).get("text") or str(item["id"])) for item in items]
        selected = target or (str(items[0]["id"]) if len(items) == 1 else choose("Choose a captured target", options))
        matches = [item for item in items if str(item["id"]) == selected]
        if len(matches) != 1:
            raise ValueError("Unknown or ambiguous target ID")
        choices = ("ask", "block")
        current = matches[0]["decision"]
        print(f"{entry['name']} · target {selected} · currently {current}")
    else:
        if target:
            raise ValueError("General protections use --risk, not --target")
        items = value.get("risks", [])
        options = [(item.get("id") or item.get("risk"), item.get("description") or item.get("reason") or item.get("id") or item.get("risk")) for item in items]
        if not options:
            raise ValueError("This policy has no configurable risks")
        selected = risk or (options[0][0] if len(options) == 1 else choose("Choose a risk", options))
        if selected not in {key for key, _ in options}:
            raise ValueError("Unknown risk ID")
        choices = ("ask", "block", "allow")
        current = project.snapshot(agent).config.skill_policies.get(entry["id"], {}).get(selected, "ask")
        print(f"{entry['name']} · risk {selected} · currently {current}")
    if decision is None:
        decision = choose("Choose a decision", [(choice, {"ask": "Ask for human review", "block": "Block the action", "allow": "Allow this risk without review"}[choice]) for choice in choices])
    if decision not in choices:
        raise ValueError("Specific targets support ask or block only; general risks support ask, block or allow")
    if entry["kind"] == "specific":
        if matches[0].get("reason") in {"Block activation of captured target.", "Ask before activation of captured target."}:
            matches[0]["reason"] = ("Block" if decision == "block" else "Ask before") + " activation of captured target."
        matches[0]["decision"] = decision
    else:
        configuration = project.document()
        existing = configuration["agents"][agent].get("decisions", {}).get(entry["id"], {})
        if selected in existing:
            from .revisions import save_configuration
            expected = digest(configuration)
            existing[selected] = decision
            result = save_configuration(project, configuration, expected=expected)
            return {**result, "path": str(project.path), "affected_agents": [agent]}
        value.setdefault("acira_decisions", {})[selected] = decision
    print("File assignments affected: " + ", ".join(entry["affected_agents"]))
    if entry["id"] in project.snapshot(agent).profile.get("disabled_policies", []):
        print("This policy is disabled for this agent. Use acira policy enable " + entry["id"] + " to apply it.")
    return project.save_policy(agent, Path(entry["path"]).name, value, expected=entry["hash"])


def set_enabled(project, agent, name, *, enabled):
    from .revisions import save_configuration
    entry = find_policy(project, agent, name)
    configuration = project.document()
    expected = digest(configuration)
    profile = configuration["agents"][agent]
    disabled = [item for item in profile.get("disabled_policies", []) if item != entry["id"]]
    if not enabled:
        disabled.append(entry["id"])
    profile["disabled_policies"] = disabled
    return save_configuration(project, configuration, expected=expected)
