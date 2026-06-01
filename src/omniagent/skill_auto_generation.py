from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from omniagent.graph_memory import GraphMemoryStore
from omniagent.llm.base import LLMAdapter
from omniagent.memory import FileMemoryManager
from omniagent.models import LLMResponse, SkillLifecycleStatus, TaskRequest, utc_now
from omniagent.prompting import PromptManager
from omniagent.skill_lifecycle import SkillLifecycleManager
from omniagent.skills.loader import SkillLoader
from omniagent.storage import SQLiteStore

SUPPORTED_GENERATION_TYPES = ("file", "python", "browser", "api")

SKILL_GENERATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "main_code": {"type": "string"},
        "test_code": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["main_code", "test_code", "reasoning"],
}


def _safe_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_") or "generated_skill"


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = [int(item) for item in str(value).split(".")[:3] if item.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _bump_patch(value: str | None) -> str:
    major, minor, patch = _version_tuple(value or "0.0.0")
    return f"{major}.{minor}.{patch + 1}"


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


class AutoSkillGenerator:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        workspace_root: Path,
        skill_loader: SkillLoader,
        skill_lifecycle: SkillLifecycleManager,
        memory_manager: FileMemoryManager,
        graph_memory: GraphMemoryStore,
        llm: LLMAdapter | None = None,
        prompts: PromptManager | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = workspace_root.resolve()
        self.project_root = self.workspace_root.parent
        self.skill_loader = skill_loader
        self.skill_lifecycle = skill_lifecycle
        self.memory_manager = memory_manager
        self.graph_memory = graph_memory
        self.llm = llm
        self.prompts = prompts
        self.generated_root = self.project_root / "skills" / "generated" / "auto"
        self.generated_root.mkdir(parents=True, exist_ok=True)

    def infer_generation_type(
        self,
        *,
        skill_name: str | None,
        executor_hint: str | None,
        request: TaskRequest,
        payload: dict[str, Any],
    ) -> str | None:
        if executor_hint in SUPPORTED_GENERATION_TYPES:
            return executor_hint
        if request.executor in SUPPORTED_GENERATION_TYPES:
            return str(request.executor)
        if "code" in payload:
            return "python"
        if "url" in payload:
            response_format = str(payload.get("response_format", "")).lower()
            if response_format == "json" or (skill_name and ".api." in skill_name):
                return "api"
            return "browser"
        if "action" in payload or "path" in payload:
            return "file"
        if skill_name and ".api." in skill_name:
            return "api"
        if skill_name and ".python." in skill_name:
            return "python"
        if skill_name and ".browser." in skill_name:
            return "browser"
        if skill_name and ".file." in skill_name:
            return "file"
        return None

    def generate_skill(
        self,
        *,
        skill_name: str,
        generation_type: str,
        goal: str,
        payload_example: dict[str, Any],
        trigger: str,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        generation_type = str(generation_type)
        if generation_type not in SUPPORTED_GENERATION_TYPES:
            raise ValueError(f"unsupported generation type: {generation_type}")

        analysis = self._analyze_requirement(
            skill_name=skill_name,
            generation_type=generation_type,
            goal=goal,
            payload_example=payload_example,
            trigger=trigger,
            task_id=task_id,
            node_id=node_id,
        )
        candidate_search = self._candidate_search(skill_name=skill_name, analysis=analysis)

        existing_entry = candidate_search.get("existing_registry_entry")
        if existing_entry and existing_entry.get("lifecycle_status") == SkillLifecycleStatus.ACTIVE:
            self.graph_memory.record_skill(existing_entry)
            return {
                "skill_name": skill_name,
                "status": "existing_active",
                "analysis": analysis,
                "candidate_search": candidate_search,
                "registry_entry": existing_entry,
                "draft": self.store.get_skill_draft(
                    skill_name, version=str(existing_entry["version"])
                ),
                "validation_report": self.store.latest_validation_report(
                    skill_name, version=str(existing_entry["version"])
                ),
                "approval_record": self.store.latest_approval_record(
                    skill_name, version=str(existing_entry["version"])
                ),
                "registration_record": None,
                "critic": self._critic_report(
                    analysis=analysis, validation_report=None, registration_record=None
                ),
                "generation_report_path": None,
            }

        existing_draft = self.store.get_skill_draft(skill_name)
        if existing_draft is not None and existing_draft.source_kind == "generated":
            draft = existing_draft
            skill_root = Path(draft.source_dir)
            report_path = skill_root / "generation_report.json"
        else:
            version = self._next_version(skill_name)
            skill_root = self._write_skill_package(
                skill_name=skill_name,
                version=version,
                analysis=analysis,
            )
            report_path = skill_root / "generation_report.json"
            draft = self.skill_lifecycle.register_generated_skill(
                skill_root,
                source_ref=f"{trigger}:{task_id or 'manual'}",
                metadata={
                    "generation_analysis": analysis,
                    "candidate_search": candidate_search,
                    "generation_report_path": str(report_path),
                },
            )

        validation_payload = self.skill_lifecycle.validate_skill(
            draft.skill_name, version=draft.version
        )
        validation_report = validation_payload["validation_report"]
        approval_record = validation_payload[
            "approval_record"
        ] or self.store.latest_approval_record(
            draft.skill_name,
            version=draft.version,
        )
        registration_record = None
        if validation_payload["draft"].lifecycle_status == SkillLifecycleStatus.APPROVED:
            activation = self.skill_lifecycle.activate_skill(
                draft.skill_name, version=draft.version
            )
            registration_record = activation["registration_record"]

        registry_entry = self.store.get_skill_registry_entry(draft.skill_name)
        critic = self._critic_report(
            analysis=analysis,
            validation_report=validation_report.to_dict() if validation_report else None,
            registration_record=registration_record.to_dict() if registration_record else None,
        )
        current_status = (
            str(registry_entry.lifecycle_status) if registry_entry else str(draft.lifecycle_status)
        )
        report = {
            "generated_at": utc_now(),
            "skill_name": draft.skill_name,
            "version": draft.version,
            "status": current_status,
            "trigger": trigger,
            "task_id": task_id,
            "node_id": node_id,
            "analysis": analysis,
            "candidate_search": candidate_search,
            "validation_report": validation_report.to_dict() if validation_report else None,
            "approval_record": approval_record.to_dict() if approval_record else None,
            "registration_record": registration_record.to_dict() if registration_record else None,
            "critic": critic,
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        if registry_entry is not None:
            self.graph_memory.record_skill(registry_entry.to_dict())
        self._record_learning(
            report=report, registry_entry=registry_entry.to_dict() if registry_entry else None
        )
        return {
            "skill_name": draft.skill_name,
            "status": current_status,
            "analysis": analysis,
            "candidate_search": candidate_search,
            "draft": draft,
            "registry_entry": registry_entry.to_dict() if registry_entry else None,
            "validation_report": validation_report.to_dict() if validation_report else None,
            "approval_record": approval_record.to_dict() if approval_record else None,
            "registration_record": registration_record.to_dict() if registration_record else None,
            "critic": critic,
            "generation_report_path": str(report_path),
        }

    def approve_and_activate(
        self,
        *,
        skill_name: str,
        version: str | None = None,
        approver: str,
        rationale: str,
    ) -> dict[str, Any]:
        draft = self.store.get_skill_draft(skill_name, version=version)
        if draft is None:
            raise ValueError(f"generated draft not found: {skill_name}")
        validation = self.store.latest_validation_report(skill_name, version=draft.version)
        if validation is None or not validation.passed:
            payload = self.skill_lifecycle.validate_skill(skill_name, version=draft.version)
            validation = payload["validation_report"]
        approval = self.store.latest_approval_record(skill_name, version=draft.version)
        if approval is None or approval.status not in {"approved", "not_required"}:
            approval_payload = self.skill_lifecycle.approve_skill(
                skill_name,
                version=draft.version,
                approver=approver,
                approved=True,
                rationale=rationale,
            )
            approval = approval_payload["approval_record"]
        activation = self.skill_lifecycle.activate_skill(skill_name, version=draft.version)
        registry_entry = self.store.get_skill_registry_entry(skill_name)
        if registry_entry is not None:
            self.graph_memory.record_skill(registry_entry.to_dict())
        return {
            "draft": draft,
            "validation_report": validation.to_dict()
            if hasattr(validation, "to_dict")
            else validation,
            "approval_record": approval.to_dict() if hasattr(approval, "to_dict") else approval,
            "registration_record": activation["registration_record"].to_dict(),
            "registry_entry": registry_entry.to_dict() if registry_entry else None,
        }

    def generate_for_workflow_candidate(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        generation_type = self._infer_workflow_generation_type(candidate)
        if generation_type is None:
            return None
        payload_example = self._workflow_payload_example(candidate)
        source_task_ids = list(candidate.get("source_task_ids", []))
        task_id = str(
            candidate.get("compiled_from_task_id")
            or (source_task_ids[0] if source_task_ids else "")
        )
        return self.generate_skill(
            skill_name=str(candidate["skill_name"]),
            generation_type=generation_type,
            goal=str(candidate.get("goal", candidate["skill_name"])),
            payload_example=payload_example,
            trigger="workflow_solidification",
            task_id=task_id or None,
            node_id=None,
        )

    def _analyze_requirement(
        self,
        *,
        skill_name: str,
        generation_type: str,
        goal: str,
        payload_example: dict[str, Any],
        trigger: str,
        task_id: str | None,
        node_id: str | None,
    ) -> dict[str, Any]:
        permissions_map = {
            "file": [
                "filesystem.write"
                if str(payload_example.get("action", "read")) in {"write", "append"}
                else "filesystem.read"
            ],
            "python": ["code_execution"],
            "browser": [
                "network"
                if str(payload_example.get("url", "")).startswith(("http://", "https://"))
                else "filesystem.read"
            ],
            "api": ["network"],
        }
        input_keys = sorted(str(key) for key in payload_example.keys())
        output_keys_map = {
            "file": ["path", "size", "entries", "content"],
            "python": ["result", "stdout"],
            "browser": ["url", "status", "body", "source"],
            "api": ["url", "status", "body", "json", "source"],
        }
        risk = "low"
        if generation_type in {"browser", "api"}:
            risk = "medium"
        if generation_type == "python":
            risk = "high"
        return {
            "skill_name": skill_name,
            "skill_goal": goal,
            "skill_type": generation_type,
            "required_inputs": input_keys or self._default_input_keys(generation_type),
            "expected_outputs": output_keys_map[generation_type],
            "required_permissions": permissions_map[generation_type],
            "estimated_risk": risk,
            "dependencies": [],
            "trigger": trigger,
            "task_id": task_id,
            "node_id": node_id,
            "payload_example": dict(payload_example),
        }

    def _candidate_search(self, *, skill_name: str, analysis: dict[str, Any]) -> dict[str, Any]:
        entry = self.store.get_skill_registry_entry(skill_name)
        draft = self.store.get_skill_draft(skill_name)
        related_workflows = self.memory_manager.recall_workflows(analysis["skill_goal"], limit=3)
        graph_skills = self.graph_memory.recommend_skills_for_goal(
            analysis["skill_goal"],
            executor_hint=analysis["skill_type"]
            if analysis["skill_type"] in {"file", "python", "browser"}
            else None,
            limit=3,
        )
        decision = "generate"
        if entry is not None and entry.lifecycle_status == SkillLifecycleStatus.ACTIVE:
            decision = "reuse_active"
        elif draft is not None:
            decision = "reuse_draft"
        return {
            "decision": decision,
            "existing_registry_entry": entry.to_dict() if entry else None,
            "existing_draft": draft.to_dict() if draft else None,
            "workflow_hits": related_workflows,
            "graph_skill_hits": graph_skills,
        }

    def _next_version(self, skill_name: str) -> str:
        versions = [draft.version for draft in self.store.list_skill_drafts(skill_name)]
        entry = self.store.get_skill_registry_entry(skill_name)
        if entry is not None:
            versions.append(entry.version)
        latest = max(versions, key=_version_tuple) if versions else None
        return _bump_patch(latest)

    def _generate_code_via_llm(self, analysis: dict[str, Any]) -> tuple[str, str] | None:
        """Try to generate skill code via LLM. Returns (main_code, test_code) or None on failure."""
        if self.llm is None or self.prompts is None:
            return None
        try:
            prompt = self.prompts.load("skill_generator_v1")
            request_payload = {
                "goal": analysis["skill_goal"],
                "skill_type": analysis["skill_type"],
                "required_inputs": analysis["required_inputs"],
                "expected_outputs": analysis["expected_outputs"],
                "permissions": analysis["required_permissions"],
                "payload_example": analysis["payload_example"],
            }
            response: LLMResponse = self.llm.generate_structured(
                prompt_name="skill_generator_v1",
                system_prompt=prompt,
                request_payload=request_payload,
                schema=SKILL_GENERATOR_SCHEMA,
            )
            out = response.structured_output
            main_code = out.get("main_code", "")
            test_code = out.get("test_code", "")
            if main_code and "def run(" in main_code:
                return main_code, test_code
        except Exception:
            pass
        return None

    def _write_skill_package(
        self,
        *,
        skill_name: str,
        version: str,
        analysis: dict[str, Any],
    ) -> Path:
        skill_dir = self.generated_root / _safe_component(skill_name) / version
        skill_dir.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest(skill_name=skill_name, version=version, analysis=analysis)
        (skill_dir / "skill.yaml").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        # Try LLM-based code generation, fall back to templates
        llm_result = self._generate_code_via_llm(analysis)
        if llm_result is not None:
            main_code, test_code = llm_result
        else:
            main_code = self._main_template(analysis["skill_type"])
            test_code = self._test_template(
                skill_name=skill_name, generation_type=analysis["skill_type"]
            )
        (skill_dir / "main.py").write_text(main_code, encoding="utf-8")
        tests_dir = skill_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_skill.py").write_text(test_code, encoding="utf-8")
        (skill_dir / "README.md").write_text(
            self._readme(skill_name=skill_name, version=version, analysis=analysis),
            encoding="utf-8",
        )
        return skill_dir

    def _manifest(
        self, *, skill_name: str, version: str, analysis: dict[str, Any]
    ) -> dict[str, Any]:
        input_schema_map = {
            "file": {
                "action": "string",
                "path": "string",
                "content": "string",
                "overwrite": "boolean",
            },
            "python": {"code": "string"},
            "browser": {"url": "string"},
            "api": {"url": "string", "method": "string", "response_format": "string"},
        }
        output_schema_map = {
            "file": {"path": "string", "size": "integer", "content": "string", "entries": "array"},
            "python": {"result": "object", "stdout": "string"},
            "browser": {"url": "string", "status": "integer", "body": "string", "source": "string"},
            "api": {
                "url": "string",
                "status": "integer",
                "body": "string",
                "json": "object",
                "source": "string",
            },
        }
        return {
            "name": skill_name,
            "version": version,
            "description": f"Auto generated {analysis['skill_type']} skill for: {analysis['skill_goal']}",
            "input_schema": input_schema_map[analysis["skill_type"]],
            "output_schema": output_schema_map[analysis["skill_type"]],
            "permission_requirements": list(analysis["required_permissions"]),
            "dependency_requirements": [],
            "side_effects": [],
            "sandbox_level": "workspace",
            "tags": ["generated", analysis["skill_type"], "auto-generated"],
            "source_relation": {
                "kind": "generated",
                "trigger": analysis["trigger"],
                "task_id": analysis.get("task_id"),
                "node_id": analysis.get("node_id"),
            },
        }

    def _main_template(self, generation_type: str) -> str:
        templates = {
            "file": "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "",
                    "def run(payload, context):",
                    "    sandbox = context['sandbox']",
                    "    action = str(payload.get('action', '')).strip()",
                    "    if not action:",
                    "        return {",
                    "            'success': True,",
                    "            'status': 'completed',",
                    "            'message': 'generated file skill ready',",
                    "            'output': {'supported_actions': ['list', 'read', 'write', 'append']},",
                    "        }",
                    "    if action == 'list':",
                    "        path = str(payload.get('path', '.'))",
                    "        return {",
                    "            'success': True,",
                    "            'status': 'completed',",
                    "            'message': f'列出目录 {path} 成功',",
                    "            'output': {'entries': sandbox.list_dir(path), 'path': path},",
                    "        }",
                    "    if action == 'read':",
                    "        path = str(payload.get('path', ''))",
                    "        if not path:",
                    "            raise ValueError('file skill 需要 path')",
                    "        return {",
                    "            'success': True,",
                    "            'status': 'completed',",
                    "            'message': f'读取文件 {path} 成功',",
                    "            'output': {'content': sandbox.read_text(path), 'path': path},",
                    "        }",
                    "    if action in {'write', 'append'}:",
                    "        path = str(payload.get('path', ''))",
                    "        if not path:",
                    "            raise ValueError('file skill 需要 path')",
                    "        content = str(payload.get('content', ''))",
                    "        overwrite = bool(payload.get('overwrite', False))",
                    "        if action == 'append':",
                    "            try:",
                    "                existing = sandbox.read_text(path)",
                    "            except Exception:",
                    "                existing = ''",
                    "            content = existing + content",
                    "            overwrite = True",
                    "        written = sandbox.write_text(path, content, overwrite=overwrite)",
                    "        return {",
                    "            'success': True,",
                    "            'status': 'completed',",
                    "            'message': f'写入文件 {path} 成功',",
                    "            'output': {'path': path, 'size': len(content), 'written_path': str(written)},",
                    "        }",
                    "    raise ValueError(f'不支持的 file action: {action}')",
                    "",
                ]
            ),
            "python": "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "",
                    "def run(payload, context):",
                    "    sandbox = context['sandbox']",
                    "    code = str(payload.get('code', '')).strip()",
                    "    if not code:",
                    "        return {",
                    "            'success': True,",
                    "            'status': 'completed',",
                    "            'message': 'generated python skill ready',",
                    "            'output': {'supported_inputs': ['code']},",
                    "        }",
                    "    response = sandbox.run_python(code)",
                    "    if not bool(response.get('ok')):",
                    "        return {",
                    "            'success': False,",
                    "            'status': 'failed',",
                    "            'message': str(response.get('error', 'Python 执行失败')),",
                    "            'output': {'stdout': response.get('stdout', ''), 'traceback': response.get('traceback', '')},",
                    "        }",
                    "    return {",
                    "        'success': True,",
                    "        'status': 'completed',",
                    "        'message': 'Python 执行成功',",
                    "        'output': {'result': response.get('result'), 'stdout': response.get('stdout', '')},",
                    "    }",
                    "",
                ]
            ),
            "browser": "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "",
                    "def run(payload, context):",
                    "    sandbox = context['sandbox']",
                    "    url = str(payload.get('url', '')).strip()",
                    "    if not url:",
                    "        return {",
                    "            'success': True,",
                    "            'status': 'completed',",
                    "            'message': 'generated browser skill ready',",
                    "            'output': {'supported_inputs': ['url']},",
                    "        }",
                    "    response = sandbox.fetch_url(url)",
                    "    return {",
                    "        'success': True,",
                    "        'status': 'completed',",
                    "        'message': f'抓取页面成功: {url}',",
                    "        'output': {",
                    "            'url': response.get('url'),",
                    "            'status': response.get('status'),",
                    "            'body': response.get('body'),",
                    "            'source': response.get('source'),",
                    "        },",
                    "    }",
                    "",
                ]
            ),
            "api": "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "import json",
                    "",
                    "",
                    "def run(payload, context):",
                    "    sandbox = context['sandbox']",
                    "    url = str(payload.get('url', '')).strip()",
                    "    if not url:",
                    "        return {",
                    "            'success': True,",
                    "            'status': 'completed',",
                    "            'message': 'generated api skill ready',",
                    "            'output': {'supported_inputs': ['url', 'method', 'response_format']},",
                    "        }",
                    "    response = sandbox.fetch_url(url)",
                    "    body = str(response.get('body', ''))",
                    "    parsed = None",
                    "    try:",
                    "        parsed = json.loads(body) if body else None",
                    "    except json.JSONDecodeError:",
                    "        parsed = None",
                    "    return {",
                    "        'success': True,",
                    "        'status': 'completed',",
                    "        'message': f'API 请求成功: {url}',",
                    "        'output': {",
                    "            'url': response.get('url'),",
                    "            'status': response.get('status'),",
                    "            'body': body,",
                    "            'json': parsed,",
                    "            'source': response.get('source'),",
                    "        },",
                    "    }",
                    "",
                ]
            ),
        }
        return templates[generation_type]

    def _test_template(self, *, skill_name: str, generation_type: str) -> str:
        body = {
            "file": [
                "        result = module.run({'action': 'write', 'path': 'tmp.txt', 'content': 'hello', 'overwrite': True}, {'sandbox': FakeSandbox()})",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['path'], 'tmp.txt')",
            ],
            "python": [
                "        result = module.run({'code': \"result = {'ok': True}\"}, {'sandbox': FakeSandbox()})",
                "        self.assertTrue(result['success'])",
                "        self.assertIn('result', result['output'])",
            ],
            "browser": [
                "        result = module.run({'url': 'https://example.com'}, {'sandbox': FakeSandbox()})",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['status'], 200)",
            ],
            "api": [
                "        result = module.run({'url': 'https://example.com/api', 'response_format': 'json'}, {'sandbox': FakeSandbox()})",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['json']['ok'], True)",
            ],
        }[generation_type]
        fetch_body = '{"ok": true}' if generation_type == "api" else "body"
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def list_dir(self, path):",
                "        return ['a.txt']",
                "",
                "    def read_text(self, path):",
                "        return 'hello'",
                "",
                "    def write_text(self, path, content, overwrite=False):",
                "        return Path(path)",
                "",
                "    def run_python(self, code):",
                "        return {'ok': True, 'result': {'ok': True}, 'stdout': 'ok'}",
                "",
                "    def fetch_url(self, url):",
                f"        return {{'url': url, 'status': 200, 'body': {fetch_body!r}, 'source': 'test'}}",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'generated_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class GeneratedSkillTest(unittest.TestCase):",
                "    def test_generated_skill(self):",
                "        module = _load_module()",
                *body,
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _readme(self, *, skill_name: str, version: str, analysis: dict[str, Any]) -> str:
        return "\n".join(
            [
                f"# {skill_name}",
                "",
                f"- version: {version}",
                f"- type: {analysis['skill_type']}",
                f"- trigger: {analysis['trigger']}",
                f"- goal: {analysis['skill_goal']}",
                f"- permissions: {', '.join(analysis['required_permissions'])}",
                "",
                "This package was generated from a controlled template by OmniAgent.",
                "",
            ]
        )

    def _critic_report(
        self,
        *,
        analysis: dict[str, Any],
        validation_report: dict[str, Any] | None,
        registration_record: dict[str, Any] | None,
    ) -> dict[str, Any]:
        passed = bool(validation_report and validation_report.get("passed"))
        activation = registration_record.get("status") if registration_record else "pending"
        summary = "模板化生成结果通过基础校验"
        if not passed:
            summary = "模板化生成结果未通过基础校验"
        elif activation != "active":
            summary = "模板化生成结果已通过校验，但仍等待审批或激活"
        return {
            "passed": passed,
            "summary": summary,
            "activation_status": activation,
            "risk": analysis["estimated_risk"],
            "review_source": "template_critic",
        }

    def _record_learning(
        self, *, report: dict[str, Any], registry_entry: dict[str, Any] | None
    ) -> None:
        entry_id = f"skillgen_{_safe_component(report['skill_name'])}_{report['version']}"
        text = "\n".join(
            [
                report["analysis"]["skill_goal"],
                report["analysis"]["skill_type"],
                report["critic"]["summary"],
                str(report["validation_report"]),
            ]
        )
        self.memory_manager.write_vector_entry(
            entry_id=entry_id,
            text=text,
            source_type="generated_skill",
            metadata={
                "skill_name": report["skill_name"],
                "version": report["version"],
                "status": registry_entry["lifecycle_status"]
                if registry_entry
                else report["status"],
                "trigger": report["trigger"],
            },
        )
        workflow_path = self.memory_manager.workflow_root / f"{entry_id}.json"
        workflow_path.write_text(
            json.dumps(
                {
                    "task_id": report.get("task_id"),
                    "goal": report["analysis"]["skill_goal"],
                    "plan_summary": f"auto-generated {report['analysis']['skill_type']} skill",
                    "result_message": report["critic"]["summary"],
                    "skill_name": report["skill_name"],
                    "status": registry_entry["lifecycle_status"]
                    if registry_entry
                    else report["status"],
                    "created_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _default_input_keys(self, generation_type: str) -> list[str]:
        return {
            "file": ["action", "path", "content"],
            "python": ["code"],
            "browser": ["url"],
            "api": ["url", "method"],
        }[generation_type]

    def _infer_workflow_generation_type(self, candidate: dict[str, Any]) -> str | None:
        template = self._load_workflow_template(candidate)
        nodes = list(template.get("template_nodes", []))
        if not nodes:
            return None
        node_types = {self._workflow_node_type(node) for node in nodes}
        node_types.discard(None)
        if len(node_types) != 1:
            return None
        generation_type = next(iter(node_types))
        return generation_type if generation_type in SUPPORTED_GENERATION_TYPES else None

    def _workflow_payload_example(self, candidate: dict[str, Any]) -> dict[str, Any]:
        template = self._load_workflow_template(candidate)
        nodes = list(template.get("template_nodes", []))
        if not nodes:
            return {}
        payload = dict(nodes[0].get("input_payload", {}))
        for parameter in template.get("parameters", []):
            node_id = str(parameter.get("node_id", ""))
            field_path = str(parameter.get("field_path", ""))
            if node_id != str(nodes[0].get("node_id")) or not field_path:
                continue
            _set_path(payload, field_path, parameter.get("default"))
        return payload

    def _load_workflow_template(self, candidate: dict[str, Any]) -> dict[str, Any]:
        template_path = Path(str(candidate.get("template_path", "")))
        if not template_path.exists():
            return {}
        return json.loads(template_path.read_text(encoding="utf-8"))

    def _workflow_node_type(self, node: dict[str, Any]) -> str | None:
        payload = dict(node.get("input_payload", {}))
        executor_hint = str(node.get("executor_hint") or "")
        selected_skill = str(
            node.get("metadata", {}).get("selected_skill") or node.get("skill_name") or ""
        )
        if selected_skill.startswith("builtin."):
            executor_hint = {
                "builtin.file.basic": "file",
                "builtin.python.basic": "python",
                "builtin.browser.basic": "browser",
            }.get(selected_skill, executor_hint)
        if executor_hint == "browser" and str(payload.get("response_format", "")).lower() == "json":
            return "api"
        return executor_hint or self.infer_generation_type(
            skill_name=selected_skill or None,
            executor_hint=None,
            request=TaskRequest(goal="", payload=payload, executor="auto"),
            payload=payload,
        )
