from __future__ import annotations

import json
import re
from hashlib import sha1
from pathlib import Path
from typing import Any

from omniagent.graph_memory import GraphMemoryStore
from omniagent.models import ExecutionResult, PlanResult, Task, TaskNodeType, TaskStatus, utc_now

PROMOTION_MIN_SUCCESS_COUNT = 2
PROMOTION_MIN_SUCCESS_RATE = 0.8
PROMOTION_MAX_PARAMETER_RATIO = 0.85
PROMOTION_MAX_ACTION_NODES = 6


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return normalized.strip("-") or "workflow"


def _safe_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_") or "workflow"


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = [int(item) for item in value.split(".")[:3] if item.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _bump_patch(value: str | None) -> str:
    major, minor, patch = _version_tuple(value or "0.0.0")
    return f"{major}.{minor}.{patch + 1}"


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _flatten_scalars(payload: Any, prefix: str = "") -> dict[str, Any]:
    if _is_scalar(payload):
        return {prefix: payload} if prefix else {}
    result: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten_scalars(value, next_prefix))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            next_prefix = f"{prefix}.{index}" if prefix else str(index)
            result.update(_flatten_scalars(value, next_prefix))
    return result


def _set_path(payload: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    target = payload
    for part in parts[:-1]:
        if isinstance(target, list):
            target = target[int(part)]
        else:
            target = target.setdefault(part, {})
    leaf = parts[-1]
    if isinstance(target, list):
        target[int(leaf)] = value
    else:
        target[leaf] = value


def _parameter_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


class WorkflowCompiler:
    def __init__(self, workspace_root: Path, graph_memory: GraphMemoryStore | None = None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.workflow_root = self.workspace_root / "WORKFLOWS"
        self.project_root = self.workspace_root.parent
        self.generated_root = self.project_root / "skills" / "generated"
        self.registry_root = self.project_root / "skills" / "registry"
        self.generated_root.mkdir(parents=True, exist_ok=True)
        self.registry_root.mkdir(parents=True, exist_ok=True)
        self.graph_memory = graph_memory
        self.registry_path = self.registry_root / "workflow_candidates.json"

    def compile_for_task(
        self,
        *,
        task: Task,
        plan: PlanResult,
        result: ExecutionResult,
    ) -> dict[str, Any] | None:
        if task.status != TaskStatus.COMPLETED or not result.success:
            return None

        current = self._load_workflow(task.task_id)
        if current is None:
            return None
        if not self._is_candidate_eligible(current):
            return None

        all_workflows = self._load_all_workflows()
        matching = [
            item for item in all_workflows if item["signature_hash"] == current["signature_hash"]
        ]
        success_items = [
            item for item in matching if str(item.get("status")) == TaskStatus.COMPLETED
        ]
        total_runs = len(matching)
        success_count = len(success_items)
        success_rate = success_count / total_runs if total_runs else 0.0
        stability = success_count / max(len({item["signature_hash"] for item in matching}) or 1, 1)

        template = self._build_template(success_items or [current])
        total_scalar_fields = max(template["total_scalar_fields"], 1)
        parameter_ratio = len(template["parameters"]) / total_scalar_fields
        if success_count < PROMOTION_MIN_SUCCESS_COUNT:
            return None
        if success_rate < PROMOTION_MIN_SUCCESS_RATE:
            return None
        if parameter_ratio > PROMOTION_MAX_PARAMETER_RATIO:
            return None

        existing = self._registry_by_signature().get(current["signature_hash"])
        version = _bump_patch(existing.get("version") if existing else "0.0.0")
        goal_slug = _slugify(task.goal)
        short_hash = current["signature_hash"][:8]
        skill_name = f"generated.workflow.{goal_slug}.{short_hash}"
        candidate_dir = self.generated_root / f"{goal_slug}_{short_hash}"
        candidate_dir.mkdir(parents=True, exist_ok=True)

        permissions = sorted(self._infer_permissions(current["nodes"]))
        candidate = {
            "signature_hash": current["signature_hash"],
            "skill_name": skill_name,
            "display_name": f"Workflow Candidate {goal_slug}-{short_hash}",
            "goal": task.goal,
            "plan_summary": plan.summary,
            "status": "draft",
            "version": version,
            "promotion_source": "system_suggestion",
            "source_task_ids": sorted({item["task_id"] for item in success_items}),
            "source_workflow_ids": sorted({item["task_id"] for item in success_items}),
            "compiled_from_task_id": task.task_id,
            "success_count": success_count,
            "total_runs": total_runs,
            "success_rate": round(success_rate, 3),
            "stability": round(stability, 3),
            "parameter_count": len(template["parameters"]),
            "parameter_ratio": round(parameter_ratio, 3),
            "permission_requirements": permissions,
            "candidate_dir": str(candidate_dir),
            "manifest_path": str(candidate_dir / "skill.yaml"),
            "main_path": str(candidate_dir / "main.py"),
            "template_path": str(candidate_dir / "template.json"),
            "generated_at": utc_now(),
            "promotion_reason": (
                f"同结构 workflow 成功 {success_count} 次 / 共 {total_runs} 次，"
                f"success_rate={success_rate:.2f}，参数化字段 {len(template['parameters'])} 个"
            ),
            "template_parameters": list(template["parameters"]),
            "template_node_count": len(template["template_nodes"]),
            "source_relation": {
                "kind": "workflow_compiled",
                "compiled_from_graph_id": plan.graph.graph_id,
                "compiled_from_task_id": task.task_id,
            },
        }
        self._write_candidate_files(candidate, template)
        self._update_registry(candidate)
        if self.graph_memory is not None:
            self.graph_memory.record_workflow_candidate(candidate)
        return candidate

    def list_candidates(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        items = list(self._load_registry())
        if task_id is None:
            return items
        return [item for item in items if task_id in item.get("source_task_ids", [])]

    def get_candidate(
        self, skill_name: str, *, version: str | None = None
    ) -> dict[str, Any] | None:
        matches = [item for item in self._load_registry() if item.get("skill_name") == skill_name]
        if version is not None:
            matches = [item for item in matches if item.get("version") == version]
        if not matches:
            return None
        matches.sort(key=lambda item: item.get("generated_at", ""), reverse=True)
        return matches[0]

    def update_candidate_status(
        self,
        skill_name: str,
        *,
        version: str | None = None,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        items = self._load_registry()
        target: dict[str, Any] | None = None
        for item in items:
            if item.get("skill_name") != skill_name:
                continue
            if version is not None and item.get("version") != version:
                continue
            target = item
            break
        if target is None:
            return None
        target["status"] = status
        if metadata:
            target.update(metadata)
        payload = {
            "updated_at": utc_now(),
            "candidates": sorted(
                items, key=lambda item: item.get("generated_at", ""), reverse=True
            ),
        }
        self.registry_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return target

    def _load_workflow(self, task_id: str) -> dict[str, Any] | None:
        path = self.workflow_root / f"{task_id}.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["signature_hash"] = self._signature_hash(payload)
        return payload

    def _load_all_workflows(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for file_path in self.workflow_root.glob("*.json"):
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            payload["signature_hash"] = self._signature_hash(payload)
            items.append(payload)
        return items

    def _signature_hash(self, workflow: dict[str, Any]) -> str:
        nodes = []
        for raw_node in workflow.get("nodes", []):
            node_type = str(raw_node.get("type", "action"))
            if node_type == TaskNodeType.FINALIZE.value:
                continue
            payload = dict(raw_node.get("input_payload", {}))
            nodes.append(
                {
                    "type": node_type,
                    "executor_hint": raw_node.get("executor_hint"),
                    "skill_name": raw_node.get("metadata", {}).get("selected_skill")
                    or raw_node.get("skill_name"),
                    "action": payload.get("action"),
                    "dependencies": list(raw_node.get("dependencies", [])),
                }
            )
        raw = json.dumps(nodes, ensure_ascii=False, sort_keys=True)
        return sha1(raw.encode("utf-8")).hexdigest()

    def _is_candidate_eligible(self, workflow: dict[str, Any]) -> bool:
        nodes = [
            item
            for item in workflow.get("nodes", [])
            if str(item.get("type")) != TaskNodeType.FINALIZE.value
        ]
        if not nodes or len(nodes) > PROMOTION_MAX_ACTION_NODES:
            return False
        if str(workflow.get("status")) != TaskStatus.COMPLETED:
            return False
        for node in nodes:
            node_type = str(node.get("type"))
            if node_type in {
                TaskNodeType.APPROVAL.value,
                TaskNodeType.DECISION.value,
                TaskNodeType.WAIT.value,
                TaskNodeType.REPLAN.value,
            }:
                return False
            selected_skill = node.get("metadata", {}).get("selected_skill") or node.get(
                "skill_name"
            )
            if selected_skill and not str(selected_skill).startswith("builtin."):
                return False
        return True

    def _build_template(self, workflows: list[dict[str, Any]]) -> dict[str, Any]:
        base = max(workflows, key=lambda item: item.get("created_at", ""))
        base_nodes = [
            json.loads(json.dumps(node, ensure_ascii=False))
            for node in base.get("nodes", [])
            if str(node.get("type")) != TaskNodeType.FINALIZE.value
        ]
        parameters: list[dict[str, Any]] = []
        total_scalar_fields = 0
        comparable_nodes = [
            [
                node
                for node in workflow.get("nodes", [])
                if str(node.get("type")) != TaskNodeType.FINALIZE.value
            ]
            for workflow in workflows
        ]

        for node_index, base_node in enumerate(base_nodes):
            path_values: dict[str, list[Any]] = {}
            for workflow_nodes in comparable_nodes:
                if node_index >= len(workflow_nodes):
                    continue
                for path, value in _flatten_scalars(
                    workflow_nodes[node_index].get("input_payload", {})
                ).items():
                    path_values.setdefault(path, []).append(value)
            total_scalar_fields += len(path_values)
            for path, values in path_values.items():
                unique_values = {
                    json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values
                }
                if len(unique_values) <= 1:
                    continue
                parameter_name = _safe_component(f"{base_node['node_id']}_{path.replace('.', '_')}")
                default_value = values[-1]
                _set_path(base_node["input_payload"], path, f"{{{{{parameter_name}}}}}")
                parameters.append(
                    {
                        "name": parameter_name,
                        "node_id": base_node["node_id"],
                        "field_path": path,
                        "type": _parameter_type(default_value),
                        "default": default_value,
                        "examples": values[: min(3, len(values))],
                    }
                )
        return {
            "template_nodes": base_nodes,
            "parameters": parameters,
            "total_scalar_fields": total_scalar_fields,
        }

    def _infer_permissions(self, nodes: list[dict[str, Any]]) -> set[str]:
        permissions: set[str] = set()
        for node in nodes:
            payload = dict(node.get("input_payload", {}))
            executor_hint = str(node.get("executor_hint") or "")
            selected_skill = node.get("metadata", {}).get("selected_skill") or node.get(
                "skill_name"
            )
            if selected_skill and str(selected_skill).startswith("builtin."):
                executor_hint = {
                    "builtin.file.basic": "file",
                    "builtin.python.basic": "python",
                    "builtin.browser.basic": "browser",
                }.get(str(selected_skill), executor_hint)
            if executor_hint == "python":
                permissions.add("code_execution")
                continue
            if executor_hint == "browser":
                url = str(payload.get("url", ""))
                permissions.add(
                    "network" if url.startswith(("http://", "https://")) else "filesystem.read"
                )
                continue
            action = str(payload.get("action", "read"))
            if action in {"write", "append"}:
                permissions.add("filesystem.write")
            elif action in {"delete", "remove"} or bool(payload.get("overwrite")):
                permissions.add("filesystem.delete")
            else:
                permissions.add("filesystem.read")
        return permissions or {"filesystem.read"}

    def _write_candidate_files(self, candidate: dict[str, Any], template: dict[str, Any]) -> None:
        candidate_dir = Path(candidate["candidate_dir"])
        manifest = {
            "name": candidate["skill_name"],
            "version": candidate["version"],
            "description": f"Draft skill compiled from repeated workflow: {candidate['goal']}",
            "input_schema": {item["name"]: item["type"] for item in template["parameters"]},
            "output_schema": {"template": "object", "received_payload": "object"},
            "permission_requirements": list(candidate["permission_requirements"]),
            "dependency_requirements": [],
            "side_effects": ["workflow_compiled_draft"],
            "sandbox_level": "workspace",
            "tags": ["generated", "workflow-compiled", "draft"],
            "draft": True,
            "source_relation": candidate["source_relation"],
        }
        (candidate_dir / "skill.yaml").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        main_py = "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import json",
                "from pathlib import Path",
                "",
                "",
                "def run(payload, context):",
                "    template_path = Path(__file__).with_name('template.json')",
                "    template = json.loads(template_path.read_text(encoding='utf-8'))",
                "    return {",
                "        'success': False,",
                "        'status': 'draft',",
                "        'message': 'workflow-compiled draft skill is not activated yet',",
                "        'output': {'template': template, 'received_payload': payload},",
                "    }",
                "",
            ]
        )
        (candidate_dir / "main.py").write_text(main_py, encoding="utf-8")
        (candidate_dir / "template.json").write_text(
            json.dumps(
                {
                    "candidate": candidate,
                    "template_nodes": template["template_nodes"],
                    "parameters": template["parameters"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        readme = "\n".join(
            [
                f"# {candidate['skill_name']}",
                "",
                f"- status: {candidate['status']}",
                f"- version: {candidate['version']}",
                f"- goal: {candidate['goal']}",
                f"- promotion_source: {candidate['promotion_source']}",
                f"- success_count: {candidate['success_count']}",
                f"- total_runs: {candidate['total_runs']}",
                f"- success_rate: {candidate['success_rate']}",
                f"- parameter_count: {candidate['parameter_count']}",
                "",
                "## Source Workflows",
                *[f"- {task_id}" for task_id in candidate["source_task_ids"]],
                "",
                "## Parameters",
                *[
                    f"- {item['name']} ({item['type']}): field={item['field_path']} default={item['default']}"
                    for item in template["parameters"]
                ],
                "",
                "## Note",
                "This package is a draft artifact generated from workflow promotion. It is not active in the runtime registry.",
            ]
        )
        (candidate_dir / "README.md").write_text(readme + "\n", encoding="utf-8")

    def _registry_by_signature(self) -> dict[str, dict[str, Any]]:
        return {item["signature_hash"]: item for item in self._load_registry()}

    def _load_registry(self) -> list[dict[str, Any]]:
        if not self.registry_path.exists():
            return []
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return list(payload.get("candidates", []))
        if isinstance(payload, list):
            return payload
        return []

    def _update_registry(self, candidate: dict[str, Any]) -> None:
        existing = self._registry_by_signature()
        current = existing.get(candidate["signature_hash"])
        history = list(current.get("version_history", [])) if current else []
        if current and current.get("version"):
            history.append(current["version"])
        candidate["version_history"] = sorted(set(history))
        existing[candidate["signature_hash"]] = candidate
        payload = {
            "updated_at": utc_now(),
            "candidates": sorted(
                existing.values(), key=lambda item: item["generated_at"], reverse=True
            ),
        }
        self.registry_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
