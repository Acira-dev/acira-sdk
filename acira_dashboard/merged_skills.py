from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from acira_cdp_proxy.general_skills import GeneralSkill
from acira_cdp_proxy.redaction import redact_value
from acira_cdp_proxy.skills import SpecificSkill


MERGED_SKILL_SCHEMA = "acira.merged_skill.v1"
STRICT_DECISION_ORDER = {"allow": 0, "ask": 1, "block": 2}


class MergedSkillError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class MergedSkillStore:
    def __init__(self, *, production_dir: Path) -> None:
        self.production_dir = production_dir
        self.general_dir = production_dir / "skills" / "general"
        self.specific_dir = production_dir / "skills" / "specific"
        self.merged_dir = production_dir / "skills" / "merged"

    def list(self) -> list[dict[str, Any]]:
        return [_summary_for(path) for path in _json_files(self.merged_dir)]

    def get(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        data = _read_json_object(path)
        compiled = self.compile(data, origin=path.name)
        return {"name": path.name, "data": redact_value({**data, "compiled": compiled}), "valid": True}

    def put(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        path = self._path(name, require_exists=False)
        prepared = self.prepare(data, origin=path.name)
        _atomic_write_json(path, prepared)
        return {"name": path.name, "data": redact_value(prepared), "valid": True}

    def delete(self, name: str) -> None:
        path = self._path(name)
        path.unlink()

    def prepare(self, data: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise MergedSkillError("Merged skill must be a JSON object.")
        if str(data.get("schema") or "") != MERGED_SKILL_SCHEMA:
            raise MergedSkillError(f"Unsupported merged skill schema: {data.get('schema')!r}")
        merged_id = str(data.get("id") or origin or "").strip()
        if not merged_id:
            raise MergedSkillError("Merged skill is missing id.")
        components = _dedupe_components(_components(data))
        if not components:
            raise MergedSkillError("Merged skill must include at least one component.")
        compiled = self.compile(data, origin=origin)
        prepared = dict(data)
        prepared["id"] = merged_id
        prepared["name"] = str(data.get("name") or merged_id)
        prepared["components"] = components
        prepared["compiled"] = compiled
        return prepared

    def compile(self, data: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        state = _CompileState()
        self._compile_into(data, state, stack=(origin or str(data.get("id") or "<inline>"),))
        return state.to_metadata()

    def expand_config_skills(self, skill_names: list[str]) -> dict[str, Any]:
        state = _CompileState()
        for name in skill_names:
            data = _read_json_object(self._path(name))
            self._compile_into(data, state, stack=(name,))
        return state.to_metadata()

    def _compile_into(self, data: dict[str, Any], state: "_CompileState", *, stack: tuple[str, ...]) -> None:
        if str(data.get("schema") or "") != MERGED_SKILL_SCHEMA:
            raise MergedSkillError(f"Unsupported merged skill schema: {data.get('schema')!r}")
        for component in _components(data):
            component_type = str(component.get("type") or "").strip().lower()
            name = str(component.get("name") or component.get("path") or "").strip()
            if component_type == "general":
                path = self._safe_component_path(self.general_dir, name)
                state.add_general(path, self.production_dir)
            elif component_type == "specific":
                path = self._safe_component_path(self.specific_dir, name)
                state.add_specific(path, self.production_dir)
            elif component_type == "merged":
                path = self._path(name)
                if path.name in stack:
                    raise MergedSkillError(f"Merged skill cycle detected: {' -> '.join((*stack, path.name))}")
                self._compile_into(_read_json_object(path), state, stack=(*stack, path.name))
            else:
                raise MergedSkillError(f"Unsupported merged skill component type: {component_type!r}")
        state.merge_policy(data.get("risk_policy"), label="risk_policy")
        state.merge_policy(data.get("page_risk_policy"), label="page_risk_policy")

    def _path(self, name: str, *, require_exists: bool = True) -> Path:
        path = _safe_json_path(self.merged_dir, name, require_exists=require_exists)
        return path

    @staticmethod
    def _safe_component_path(root: Path, name: str) -> Path:
        return _safe_json_path(root, name)


class _CompileState:
    def __init__(self) -> None:
        self.general_by_id: dict[str, dict[str, Any]] = {}
        self.specific_by_id: dict[str, dict[str, Any]] = {}
        self.specific_targets: dict[str, dict[str, Any]] = {}
        # Flat risk->decision accumulator + which skill owns each risk, so policy can be
        # emitted as per-skill blocks (page/action is implied by each risk's own context).
        self.decisions: dict[str, str] = {}
        self.risk_owner: dict[str, str] = {}

    def add_general(self, path: Path, production_dir: Path) -> None:
        skill = GeneralSkill.from_file(path)
        rel = _relative_skill_ref(path, production_dir)
        existing = self.general_by_id.get(skill.id)
        if existing and existing["path"] != rel:
            raise MergedSkillError(f"General skill id appears in multiple files: {skill.id}")
        self.general_by_id[skill.id] = {"id": skill.id, "name": skill.name, "path": rel, "source": str(path)}
        for emit in skill.emits:
            self.risk_owner[emit.risk] = skill.id

    def add_specific(self, path: Path, production_dir: Path) -> None:
        skill = SpecificSkill.from_file(path)
        rel = _relative_skill_ref(path, production_dir)
        existing = self.specific_by_id.get(skill.id)
        if existing and existing["path"] != rel:
            raise MergedSkillError(f"Specific skill id appears in multiple files: {skill.id}")
        self.specific_by_id[skill.id] = {"id": skill.id, "name": skill.name, "path": rel, "source": str(path)}
        for target in skill.targets:
            key = f"{skill.id}:{target.id}"
            target_data = asdict(target)
            existing_target = self.specific_targets.get(key)
            if existing_target is not None and existing_target != target_data:
                raise MergedSkillError(f"Conflicting duplicate specific target: {key}")
            self.specific_targets[key] = target_data

    def merge_policy(self, policy: Any, *, label: str) -> None:
        if policy is None:
            return
        if not isinstance(policy, dict):
            raise MergedSkillError(f"{label} must be an object.")
        for risk, decision in policy.items():
            normalized = str(decision).strip().lower()
            if normalized not in STRICT_DECISION_ORDER:
                raise MergedSkillError(f"Unsupported {label} decision: {decision!r}")
            current = self.decisions.get(str(risk))
            if current is None or STRICT_DECISION_ORDER[normalized] > STRICT_DECISION_ORDER[current]:
                self.decisions[str(risk)] = normalized

    def to_metadata(self) -> dict[str, Any]:
        general_skills: dict[str, dict[str, Any]] = {}
        for skill_id, info in sorted(self.general_by_id.items()):
            risks = {
                risk: decision
                for risk, decision in sorted(self.decisions.items())
                if self.risk_owner.get(risk) == skill_id
            }
            general_skills[skill_id] = {"path": info["path"], "risks": risks}
        return {
            "general_skills": general_skills,
            "specific_skills": sorted(self.specific_by_id.values(), key=lambda item: item["id"]),
            "specific_targets": sorted(
                ({"key": key, **value} for key, value in self.specific_targets.items()),
                key=lambda item: item["key"],
            ),
        }


def _components(data: dict[str, Any]) -> list[dict[str, str]]:
    raw = data.get("components")
    components: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                raise MergedSkillError("Merged skill components must be objects.")
            component_type = str(item.get("type") or "").strip().lower()
            name = str(item.get("name") or item.get("path") or "").strip()
            if not component_type or not name:
                raise MergedSkillError("Merged skill component requires type and name.")
            components.append({"type": component_type, "name": name})
    for component_type, key in (("general", "general_skills"), ("specific", "specific_skills"), ("merged", "merged_skills")):
        raw_items = data.get(key)
        if isinstance(raw_items, list):
            components.extend({"type": component_type, "name": str(item)} for item in raw_items if str(item))
    return components


def _dedupe_components(components: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for component in components:
        key = (str(component.get("type") or "").strip().lower(), str(component.get("name") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"type": key[0], "name": key[1]})
    return deduped


def _summary_for(path: Path) -> dict[str, Any]:
    try:
        data = _read_json_object(path)
        return {
            "name": path.name,
            "id": data.get("id") or path.stem,
            "display_name": data.get("name") or data.get("id") or path.stem,
            "component_count": len(_components(data)),
            "valid": str(data.get("schema") or "") == MERGED_SKILL_SCHEMA,
            "error": None,
        }
    except Exception as exc:
        return {"name": path.name, "display_name": path.stem, "valid": False, "error": str(exc)}


def _safe_json_path(root: Path, name: str, *, require_exists: bool = True) -> Path:
    text = str(name or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not text or text.startswith(".") or text.endswith("."):
        raise MergedSkillError("Invalid JSON file name.")
    if "/" in text or "\\" in text or ".." in text:
        raise MergedSkillError("Path traversal is not allowed.")
    if any(char not in allowed for char in text):
        raise MergedSkillError("Invalid JSON file name.")
    if not text.endswith(".json"):
        raise MergedSkillError("Only .json files are supported.")
    root_resolved = root.resolve()
    path = (root / text).resolve()
    if path.parent != root_resolved:
        raise MergedSkillError("Path is outside the allowed dashboard directory.")
    if require_exists and not path.exists():
        raise MergedSkillError("File not found.", 404)
    return path


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise MergedSkillError("File not found.", 404)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MergedSkillError(f"Invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise MergedSkillError("File must contain a JSON object.")
    return loaded


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _json_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*.json") if path.is_file())


def _relative_skill_ref(path: Path, production_dir: Path) -> str:
    try:
        return path.resolve().relative_to((production_dir / "configs").resolve()).as_posix()
    except ValueError:
        return f"../skills/{path.parent.name}/{path.name}"
