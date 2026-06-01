from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.models import SkillContract


@dataclass(slots=True)
class SkillMetadata:
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permission_requirements: list[str]
    dependency_requirements: list[str]
    side_effects: list[str]
    sandbox_level: str
    tags: list[str]
    # META-001: Structured action contracts loaded from `skill.yaml:contracts`.
    # Empty list is allowed (legacy skills); SkillRegistry.select() will fall
    # back to tag-similarity scoring for those.
    contracts: list[SkillContract] = field(default_factory=list)


@dataclass(slots=True)
class SkillDefinition:
    metadata: SkillMetadata
    root_path: Path
    main_path: Path
    location: str


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value[0] in "{[":
        return json.loads(value)
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def load_yaml_like(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and current_list_key:
            result.setdefault(current_list_key, []).append(_parse_scalar(line[4:]))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            result[key] = []
            current_list_key = key
        else:
            result[key] = _parse_scalar(value)
            current_list_key = None
    return result


class SkillLoader:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.project_root = self.workspace_root.parent
        self.builtin_root = Path(__file__).resolve().parent.parent / "builtin_skills"

    def search_roots(self) -> list[tuple[str, Path]]:
        roots: list[tuple[str, Path]] = [
            ("builtin", self.builtin_root),
            ("workspace", self.workspace_root / "skills"),
        ]
        agents_root = self.project_root / "agents"
        if agents_root.exists():
            for skill_root in agents_root.glob("*/skills"):
                roots.append((f"agent:{skill_root.parent.name}", skill_root))
        roots.append(("shared", self.project_root / "shared" / "skills"))
        return roots

    def load_all(self) -> dict[str, SkillDefinition]:
        skills: dict[str, SkillDefinition] = {}
        for location, root in self.search_roots():
            if not root.exists():
                continue
            for manifest in root.rglob("skill.yaml"):
                skill = self._load_definition(manifest.parent, location)
                skills[skill.metadata.name] = skill
        return skills

    def load_one(self, name: str) -> SkillDefinition:
        skills = self.load_all()
        if name not in skills:
            raise OmniAgentError(ErrorCode.NOT_FOUND, f"Skill 不存在: {name}")
        return skills[name]

    def load_runner(
        self, skill: SkillDefinition
    ) -> Callable[[dict[str, Any], dict[str, Any]], Any]:
        spec = importlib.util.spec_from_file_location(
            f"omniagent_skill_{skill.metadata.name.replace('.', '_')}",
            skill.main_path,
        )
        if spec is None or spec.loader is None:
            raise OmniAgentError(ErrorCode.SKILL_ERROR, f"无法装载 Skill 模块: {skill.main_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        runner = getattr(module, "run", None)
        if runner is None or not callable(runner):
            raise OmniAgentError(
                ErrorCode.SKILL_ERROR, f"Skill 缺少 run(payload, context) 接口: {skill.main_path}"
            )
        return runner

    def _load_definition(self, skill_root: Path, location: str) -> SkillDefinition:
        manifest_path = skill_root / "skill.yaml"
        main_path = skill_root / "main.py"
        if not main_path.exists():
            raise OmniAgentError(ErrorCode.SKILL_ERROR, f"Skill 缺少 main.py: {skill_root}")

        data = load_yaml_like(manifest_path)
        contracts_raw = data.get("contracts", []) or []
        if not isinstance(contracts_raw, list):
            raise OmniAgentError(
                ErrorCode.SKILL_ERROR,
                f"Skill {skill_root} 的 contracts 字段必须是 list",
            )
        contracts: list[SkillContract] = []
        for item in contracts_raw:
            if not isinstance(item, dict):
                raise OmniAgentError(
                    ErrorCode.SKILL_ERROR,
                    f"Skill {skill_root} 的 contract 项必须是 dict，收到: {type(item).__name__}",
                )
            contracts.append(SkillContract.from_dict(item))

        metadata = SkillMetadata(
            name=str(data["name"]),
            version=str(data.get("version", "0.1.0")),
            description=str(data.get("description", "")),
            input_schema=dict(data.get("input_schema", {})),
            output_schema=dict(data.get("output_schema", {})),
            permission_requirements=list(data.get("permission_requirements", [])),
            dependency_requirements=list(data.get("dependency_requirements", [])),
            side_effects=list(data.get("side_effects", [])),
            sandbox_level=str(data.get("sandbox_level", "workspace")),
            tags=list(data.get("tags", [])),
            contracts=contracts,
        )
        return SkillDefinition(
            metadata=metadata,
            root_path=skill_root,
            main_path=main_path,
            location=location,
        )
