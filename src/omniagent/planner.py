from __future__ import annotations

import re
from typing import Any

from omniagent.llm.base import LLMAdapter
from omniagent.models import (
    BootstrapContext,
    LLMResponse,
    PlanResult,
    TaskDependency,
    TaskGraph,
    TaskNode,
    TaskNodeType,
    TaskRequest,
    utc_now,
)
from omniagent.prompting import PromptManager
from omniagent.skill_registry import build_planner_skill_context

PLANNER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "reasoning": {"type": "string"},
        "risk_level": {"type": "string"},
        "required_capabilities": {"type": "array", "items": {"type": "string"}},
        # META-003: the four legal outcomes of planning. Only "plan"
        # produces a graph; the others let the Planner exit cleanly
        # instead of fabricating a python print(...) wrapper.
        "decision": {
            "type": "string",
            "enum": ["plan", "decline_not_a_task", "clarify", "missing_capability"],
        },
        "decline_reason": {"type": ["string", "null"]},
        "clarify_question": {"type": ["string", "null"]},
        "missing_capability": {
            "type": ["object", "null"],
            "additionalProperties": True,
            "properties": {
                "action": {"type": "string"},
                "executor_class": {"type": "string"},
                "description": {"type": "string"},
            },
        },
        "graph": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "string"},
                "summary": {"type": "string"},
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "node_id": {"type": "string"},
                            "type": {"type": "string"},
                            "goal": {"type": "string"},
                            "dependencies": {"type": "array", "items": {"type": "string"}},
                            "required_skills": {"type": "array", "items": {"type": "string"}},
                            "executor_hint": {"type": ["string", "null"]},
                            "skill_name": {"type": ["string", "null"]},
                            "risk_level": {"type": "string"},
                            "retry_policy": {
                                "type": "object",
                                "additionalProperties": True,
                                "properties": {
                                    "max_retries": {"type": "integer"},
                                    "backoff_seconds": {"type": "integer"},
                                    "degrade_to": {"type": "array", "items": {"type": "string"}},
                                    "replan_on_failure": {"type": "boolean"},
                                },
                            },
                            "input_payload": {"type": "object", "additionalProperties": True},
                            "metadata": {"type": "object", "additionalProperties": True},
                        },
                        "required": [
                            "node_id",
                            "type",
                            "goal",
                            "dependencies",
                            "required_skills",
                            "risk_level",
                            "retry_policy",
                        ],
                    },
                },
                "dependencies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "source_node_id": {"type": "string"},
                            "target_node_id": {"type": "string"},
                            "condition": {"type": "string"},
                        },
                        "required": ["source_node_id", "target_node_id", "condition"],
                    },
                },
            },
            "required": ["goal", "summary", "nodes", "dependencies"],
        },
    },
    "required": ["summary", "reasoning", "risk_level", "required_capabilities", "decision"],
}


class Planner:
    def __init__(self, llm: LLMAdapter, prompts: PromptManager) -> None:
        self.llm = llm
        self.prompts = prompts

    def plan(
        self,
        *,
        task_request: TaskRequest,
        bootstrap: BootstrapContext,
        memory_summaries: list[dict[str, Any]],
        workflow_matches: list[dict[str, Any]],
        vector_matches: list[dict[str, Any]],
        failure_patterns: list[dict[str, Any]],
        graph_workflow_matches: list[dict[str, Any]],
        graph_skill_matches: list[dict[str, Any]],
        graph_failure_patterns: list[dict[str, Any]],
        recall_explanations: list[dict[str, Any]],
        skill_registry_entries: list[dict[str, Any]],
        conversation_history: list[dict[str, Any]] | None = None,
        bootstrap_documents: dict[str, Any] | None = None,
        agent_state_summary: dict[str, Any] | None = None,
        procedure_match: dict[str, Any] | None = None,
        recent_task_artifacts: list[dict[str, Any]] | None = None,
    ) -> tuple[PlanResult, Any]:
        direct_plan = self._direct_explicit_file_payload_plan(task_request)
        if direct_plan is not None:
            return direct_plan, LLMResponse(
                provider="rule_based",
                model="deterministic",
                prompt_name="planner_v1",
                request_id="rule_based_explicit_file_payload_plan",
                raw_output="{}",
                structured_output=direct_plan.to_dict(),
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                tier="rule_based",
                attempts=1,
                latency_ms=0.0,
                started_at=utc_now(),
            )

        direct_plan = self._direct_explicit_skill_payload_plan(task_request)
        if direct_plan is not None:
            return direct_plan, LLMResponse(
                provider="rule_based",
                model="deterministic",
                prompt_name="planner_v1",
                request_id="rule_based_explicit_skill_payload_plan",
                raw_output="{}",
                structured_output=direct_plan.to_dict(),
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                tier="rule_based",
                attempts=1,
                latency_ms=0.0,
                started_at=utc_now(),
            )

        direct_plan = self._direct_network_permission_plan(task_request)
        if direct_plan is not None:
            return direct_plan, LLMResponse(
                provider="rule_based",
                model="deterministic",
                prompt_name="planner_v1",
                request_id="rule_based_network_permission_plan",
                raw_output="{}",
                structured_output=direct_plan.to_dict(),
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                tier="rule_based",
                attempts=1,
                latency_ms=0.0,
                started_at=utc_now(),
            )

        direct_plan = self._direct_config_report_plan(task_request)
        if direct_plan is not None:
            return direct_plan, LLMResponse(
                provider="rule_based",
                model="deterministic",
                prompt_name="planner_v1",
                request_id="rule_based_config_report_plan",
                raw_output="{}",
                structured_output=direct_plan.to_dict(),
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                tier="rule_based",
                attempts=1,
                latency_ms=0.0,
                started_at=utc_now(),
            )

        direct_plan = self._direct_json_report_plan(task_request)
        if direct_plan is not None:
            return direct_plan, LLMResponse(
                provider="rule_based",
                model="deterministic",
                prompt_name="planner_v1",
                request_id="rule_based_json_report_plan",
                raw_output="{}",
                structured_output=direct_plan.to_dict(),
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                tier="rule_based",
                attempts=1,
                latency_ms=0.0,
                started_at=utc_now(),
            )

        direct_plan = self._direct_file_plan(
            task_request,
            recent_task_artifacts=recent_task_artifacts,
        )
        if direct_plan is not None:
            return direct_plan, LLMResponse(
                provider="rule_based",
                model="deterministic",
                prompt_name="planner_v1",
                request_id="rule_based_file_plan",
                raw_output="{}",
                structured_output=direct_plan.to_dict(),
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                tier="rule_based",
                attempts=1,
                latency_ms=0.0,
                started_at=utc_now(),
            )

        prompt = self.prompts.load("planner_v1")
        # Bootstrap 文档支持外部去重：调用方可以传入一个"精简版"（例如 {"__unchanged": true}）
        # 若未传则使用完整 bootstrap.documents
        docs_for_prompt = (
            bootstrap_documents if bootstrap_documents is not None else bootstrap.documents
        )
        request_payload = self.build_request_payload(
            task_request=task_request,
            bootstrap_documents=docs_for_prompt,
            memory_summaries=memory_summaries,
            workflow_matches=workflow_matches,
            vector_matches=vector_matches,
            failure_patterns=failure_patterns,
            graph_workflow_matches=graph_workflow_matches,
            graph_skill_matches=graph_skill_matches,
            graph_failure_patterns=graph_failure_patterns,
            recall_explanations=recall_explanations,
            skill_registry_entries=skill_registry_entries,
            conversation_history=conversation_history,
            agent_state_summary=agent_state_summary,
            procedure_match=procedure_match,
            recent_task_artifacts=recent_task_artifacts,
        )
        response = self.llm.generate_structured(
            prompt_name="planner_v1",
            system_prompt=prompt,
            request_payload=request_payload,
            schema=PLANNER_SCHEMA,
        )
        plan = PlanResult.from_dict(response.structured_output)
        # META-003: non-"plan" decisions (decline_not_a_task / clarify /
        # missing_capability) carry an empty graph on purpose. Skip the
        # override-and-validate pass below — there is no graph to touch
        # and no executor/skill injection makes sense.
        if plan.decision != "plan":
            return plan, response
        if task_request.skill_name:
            for node in plan.graph.nodes:
                if node.type.value == "action" and node.skill_name is None:
                    node.skill_name = task_request.skill_name
                    break
        if task_request.executor != "auto":
            for node in plan.graph.nodes:
                if node.type.value == "action" and node.executor_hint is None:
                    node.executor_hint = task_request.executor
                    break
        for node in plan.graph.nodes:
            if node.type.value == "action" and not node.input_payload and task_request.payload:
                node.input_payload = dict(task_request.payload)
                break
        plan.graph.validate()
        # ISSUE-006: code-level enforcement of planner_v1.md rule
        # "高风险操作（删除、覆盖）前应插入 approval 节点". Even if the LLM
        # skipped the approval step, we now materialise it in the graph so
        # the execution engine sees an explicit approval checkpoint instead
        # of silently relying on the permission-manager fallback path.
        plan.graph.enforce_destructive_approvals()
        return plan, response

    def build_request_payload(
        self,
        *,
        task_request: TaskRequest,
        bootstrap_documents: dict[str, Any],
        memory_summaries: list[dict[str, Any]],
        workflow_matches: list[dict[str, Any]],
        vector_matches: list[dict[str, Any]],
        failure_patterns: list[dict[str, Any]],
        graph_workflow_matches: list[dict[str, Any]],
        graph_skill_matches: list[dict[str, Any]],
        graph_failure_patterns: list[dict[str, Any]],
        recall_explanations: list[dict[str, Any]],
        skill_registry_entries: list[dict[str, Any]],
        conversation_history: list[dict[str, Any]] | None = None,
        agent_state_summary: dict[str, Any] | None = None,
        procedure_match: dict[str, Any] | None = None,
        recent_task_artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        skill_context = build_planner_skill_context(
            skill_registry_entries,
            task_request,
            goal=task_request.goal,
        )
        expanded_skill_manifests = skill_context["expanded_skill_manifests"]
        return {
            "task_request": self._serialize_task_request(task_request),
            "bootstrap_documents": bootstrap_documents,
            "agent_state_summary": dict(agent_state_summary or {}),
            "procedure_match": dict(procedure_match or {}),
            "conversation_history": list(conversation_history or []),
            # META-008: metadata-only manifest of prior task artifacts in
            # this thread so the Planner can decide whether to pull them.
            "recent_task_artifacts": list(recent_task_artifacts or []),
            "memory_summaries": memory_summaries,
            "workflow_matches": workflow_matches,
            "vector_matches": vector_matches,
            "failure_patterns": failure_patterns,
            "graph_workflow_matches": graph_workflow_matches,
            "graph_skill_matches": graph_skill_matches,
            "graph_failure_patterns": graph_failure_patterns,
            "recall_explanations": recall_explanations,
            "skill_capability_index": skill_context["skill_capability_index"],
            "expanded_skill_manifests": expanded_skill_manifests,
            "skill_manifest_budget": skill_context["manifest_budget"],
            # Compatibility key for the existing prompt and older diagnostics.
            "skill_registry_entries": expanded_skill_manifests,
        }

    def _direct_file_plan(
        self,
        task_request: TaskRequest,
        *,
        recent_task_artifacts: list[dict[str, Any]] | None = None,
    ) -> PlanResult | None:
        fallback_path = self._recent_context_file_path(task_request.goal, recent_task_artifacts)
        compound_plan = self._direct_replace_and_append_plan(
            task_request.goal,
            fallback_path=fallback_path,
        )
        if compound_plan is not None:
            return self._build_replace_and_append_plan(task_request, compound_plan)
        append_plan = self._direct_file_append_plan(
            task_request.goal,
            fallback_path=fallback_path,
        )
        if append_plan is not None:
            return self._build_file_append_plan(task_request, append_plan)
        write_plan = self._direct_file_write_plan(task_request.goal)
        if write_plan is not None:
            return self._build_file_write_plan(task_request, write_plan)
        field_report_plan = self._direct_file_field_report_plan(task_request.goal)
        if field_report_plan is not None:
            return self._build_file_write_plan(task_request, field_report_plan)
        line_plan = self._direct_replace_line_plan(task_request.goal, fallback_path=fallback_path)
        if line_plan is not None:
            return self._build_replace_line_plan(task_request, line_plan)
        csv_plan = self._direct_csv_report_plan(task_request)
        if csv_plan is not None:
            return self._build_csv_report_plan(task_request, csv_plan)
        return None

    def _direct_explicit_file_payload_plan(
        self, task_request: TaskRequest
    ) -> PlanResult | None:
        if task_request.executor != "file" or not task_request.payload:
            return None
        if task_request.skill_name not in {None, "", "builtin.file.basic"}:
            return None
        action = str(task_request.payload.get("action") or "").strip().lower()
        if action in {"delete", "remove"}:
            return None
        node = TaskNode(
            node_id="node_explicit_file_payload",
            type=TaskNodeType.ACTION,
            goal=f"Execute explicit file payload for: {task_request.goal}",
            executor_hint="file",
            skill_name=task_request.skill_name or None,
            input_payload=dict(task_request.payload),
        )
        return self._single_action_plan(
            task_request,
            node,
            summary="使用显式 file executor payload 直接生成执行计划",
            graph_summary="Deterministic explicit file payload plan",
            reasoning=(
                "The caller provided both executor=file and a structured payload, "
                "so the payload is the authoritative tool input."
            ),
            required_capabilities=["filesystem"],
        )

    def _direct_explicit_skill_payload_plan(
        self, task_request: TaskRequest
    ) -> PlanResult | None:
        if not task_request.skill_name or not task_request.payload:
            return None
        if not task_request.skill_name.startswith("builtin."):
            return None
        if task_request.skill_name == "builtin.file.basic":
            action = str(task_request.payload.get("action") or "").strip().lower()
            if action in {"delete", "remove"}:
                return None
        node = TaskNode(
            node_id="node_explicit_skill_payload",
            type=TaskNodeType.ACTION,
            goal=f"Execute explicit {task_request.skill_name} payload for: {task_request.goal}",
            skill_name=task_request.skill_name,
            input_payload=dict(task_request.payload),
        )
        return self._single_action_plan(
            task_request,
            node,
            summary="使用显式 skill payload 直接生成执行计划",
            graph_summary="Deterministic explicit skill payload plan",
            reasoning=(
                "The caller provided a concrete skill_name and structured payload, "
                "so the payload is the authoritative tool input."
            ),
            required_capabilities=[],
        )

    def _direct_config_report_plan(self, task_request: TaskRequest) -> PlanResult | None:
        if (
            task_request.skill_name != "builtin.data.config"
            and "builtin.data.config" not in task_request.preferred_skills
        ):
            return None
        parsed = self._parse_config_report_goal(task_request.goal)
        if parsed is None:
            return None
        path, output_path, labels = parsed
        fields = [
            {"label": label, "query": self._config_query_for_label(label)} for label in labels
        ]
        node = TaskNode(
            node_id="node_config_report",
            type=TaskNodeType.ACTION,
            goal=f"Extract config fields from {path} and write {output_path}",
            skill_name="builtin.data.config",
            input_payload={
                "action": "report",
                "path": path,
                "fields": fields,
                "output_path": output_path,
            },
        )
        return self._single_action_plan(
            task_request,
            node,
            summary="使用 Config Skill 直接生成配置报告计划",
            graph_summary="Deterministic config report plan",
            reasoning="The request explicitly asks to use builtin.data.config for a label report.",
            required_capabilities=["filesystem.read", "filesystem.write"],
        )

    def _direct_network_permission_plan(self, task_request: TaskRequest) -> PlanResult | None:
        if task_request.constraints.allow_network:
            return None
        url_match = re.search(r"https?://[^\s)>\]\"']+", task_request.goal)
        if url_match is None:
            return None
        if not re.search(
            r"\b(?:fetch|download|open|visit|request|summari[sz]e|get)\b|抓取|访问|网页|联网",
            task_request.goal,
            flags=re.IGNORECASE,
        ):
            return None
        url = url_match.group(0).rstrip(".,")
        node = TaskNode(
            node_id="node_network_permission_check",
            type=TaskNodeType.ACTION,
            goal=f"Request {url} through the controlled HTTP skill",
            skill_name="builtin.http.client",
            input_payload={"action": "get", "url": url},
            risk_level="medium",
        )
        return self._single_action_plan(
            task_request,
            node,
            summary="使用受控 HTTP Skill 生成网络权限检查计划",
            graph_summary="Deterministic network permission plan",
            reasoning=(
                "The request explicitly asks to fetch an HTTP(S) URL while network "
                "access is not enabled, so the runtime must surface a permission "
                "denial without depending on LLM planning."
            ),
            required_capabilities=["network"],
        )

    def _direct_json_report_plan(self, task_request: TaskRequest) -> PlanResult | None:
        if (
            task_request.skill_name != "builtin.data.json"
            and "builtin.data.json" not in task_request.preferred_skills
        ):
            return None
        parsed = self._parse_json_report_goal(task_request.goal)
        if parsed is None:
            return None
        path, output_path, fields = parsed
        node = TaskNode(
            node_id="node_json_report",
            type=TaskNodeType.ACTION,
            goal=f"Extract JSON fields from {path} and write {output_path}",
            skill_name="builtin.data.json",
            input_payload={
                "action": "report",
                "path": path,
                "fields": fields,
                "output_path": output_path,
            },
        )
        return self._single_action_plan(
            task_request,
            node,
            summary="使用 JSON Skill 直接生成字段报告计划",
            graph_summary="Deterministic JSON report plan",
            reasoning="The request explicitly asks to extract JSON fields and write a report.",
            required_capabilities=["filesystem.read", "filesystem.write"],
        )

    def _build_replace_line_plan(
        self,
        task_request: TaskRequest,
        line_plan: tuple[str, str, str],
    ) -> PlanResult:
        path, line_prefix, content = line_plan
        update_node = TaskNode(
            node_id="node_replace_line",
            type=TaskNodeType.ACTION,
            goal=f"Replace line starting with {line_prefix} in {path}",
            skill_name="builtin.file.basic",
            input_payload={
                "action": "replace_line",
                "path": path,
                "line_prefix": line_prefix,
                "content": content,
            },
        )
        return self._single_action_plan(
            task_request,
            update_node,
            summary="使用文件 Skill 直接替换目标行",
            graph_summary="Deterministic file line replacement plan",
            reasoning="The request explicitly asks to update one named line in one file.",
            required_capabilities=["filesystem.write"],
        )

    def _build_replace_and_append_plan(
        self,
        task_request: TaskRequest,
        compound_plan: tuple[str, str, str, str],
    ) -> PlanResult:
        path, line_prefix, replacement, append_content = compound_plan
        replace_node = TaskNode(
            node_id="node_replace_line",
            type=TaskNodeType.ACTION,
            goal=f"Replace line starting with {line_prefix} in {path}",
            skill_name="builtin.file.basic",
            input_payload={
                "action": "replace_line",
                "path": path,
                "line_prefix": line_prefix,
                "content": replacement,
            },
        )
        append_node = TaskNode(
            node_id="node_append_file",
            type=TaskNodeType.ACTION,
            goal=f"Append exact text to {path}",
            dependencies=[replace_node.node_id],
            skill_name="builtin.file.basic",
            input_payload={
                "action": "append",
                "path": path,
                "content": append_content + "\n",
            },
        )
        finalize_node = TaskNode(
            node_id="node_finalize",
            type=TaskNodeType.FINALIZE,
            goal="Summarize execution result",
            dependencies=[append_node.node_id],
        )
        graph = TaskGraph(
            goal=task_request.goal,
            summary="Deterministic file line replacement and append plan",
            nodes=[replace_node, append_node, finalize_node],
            dependencies=[
                TaskDependency(
                    source_node_id=replace_node.node_id,
                    target_node_id=append_node.node_id,
                    condition="success",
                ),
                TaskDependency(
                    source_node_id=append_node.node_id,
                    target_node_id=finalize_node.node_id,
                    condition="success",
                ),
            ],
        )
        graph.validate()
        return PlanResult(
            summary="使用文件 Skill 直接替换目标行并追加内容",
            graph=graph,
            reasoning=(
                "The request explicitly asks for one named line replacement and "
                "one append operation on the same referenced file."
            ),
            risk_level="low",
            required_capabilities=["filesystem.write"],
        )

    def _build_file_write_plan(
        self,
        task_request: TaskRequest,
        write_plan: tuple[str, str],
    ) -> PlanResult:
        path, content = write_plan
        node = TaskNode(
            node_id="node_write_file",
            type=TaskNodeType.ACTION,
            goal=f"Write exact text to {path}",
            skill_name="builtin.file.basic",
            input_payload={
                "action": "write",
                "path": path,
                "content": content,
                "overwrite": False,
            },
        )
        source_read_path = self._source_read_path(task_request.goal, target_path=path)
        if source_read_path is not None:
            return self._file_read_then_write_plan(
                task_request,
                source_read_path,
                node,
                path,
            )
        if self._requires_file_readback(task_request.goal):
            return self._file_write_then_read_plan(task_request, node, path)
        return self._single_action_plan(
            task_request,
            node,
            summary="使用文件 Skill 直接写入明确的一行内容",
            graph_summary="Deterministic exact one-line file write plan",
            reasoning="The request explicitly names a target file and exact one-line content.",
            required_capabilities=["filesystem.write"],
        )

    def _build_file_append_plan(
        self,
        task_request: TaskRequest,
        append_plan: tuple[str, str],
    ) -> PlanResult:
        path, content = append_plan
        node = TaskNode(
            node_id="node_append_file",
            type=TaskNodeType.ACTION,
            goal=f"Append exact text to {path}",
            skill_name="builtin.file.basic",
            input_payload={
                "action": "append",
                "path": path,
                "content": content,
            },
        )
        if self._requires_file_readback(task_request.goal):
            return self._file_write_then_read_plan(task_request, node, path)
        return self._single_action_plan(
            task_request,
            node,
            summary="使用文件 Skill 直接追加明确的一行内容",
            graph_summary="Deterministic exact one-line file append plan",
            reasoning="The request explicitly names an append-line update for one workspace file.",
            required_capabilities=["filesystem.write"],
        )

    def _file_read_then_write_plan(
        self,
        task_request: TaskRequest,
        source_path: str,
        action_node: TaskNode,
        target_path: str,
    ) -> PlanResult:
        read_source_node = TaskNode(
            node_id="node_read_source_file",
            type=TaskNodeType.ACTION,
            goal=f"Read {source_path} before writing {target_path}",
            skill_name="builtin.file.basic",
            input_payload={
                "action": "read",
                "path": source_path,
            },
        )
        action_node.dependencies = [read_source_node.node_id]
        nodes = [read_source_node, action_node]
        dependencies = [
            TaskDependency(
                source_node_id=read_source_node.node_id,
                target_node_id=action_node.node_id,
                condition="success",
            )
        ]
        finalize_dependency = action_node.node_id
        if self._requires_file_readback(task_request.goal):
            read_target_node = TaskNode(
                node_id="node_read_file",
                type=TaskNodeType.ACTION,
                goal=f"Read {target_path} after writing it",
                dependencies=[action_node.node_id],
                skill_name="builtin.file.basic",
                input_payload={
                    "action": "read",
                    "path": target_path,
                },
            )
            nodes.append(read_target_node)
            dependencies.append(
                TaskDependency(
                    source_node_id=action_node.node_id,
                    target_node_id=read_target_node.node_id,
                    condition="success",
                )
            )
            finalize_dependency = read_target_node.node_id
        finalize_node = TaskNode(
            node_id="node_finalize",
            type=TaskNodeType.FINALIZE,
            goal="Summarize execution result",
            dependencies=[finalize_dependency],
        )
        nodes.append(finalize_node)
        dependencies.append(
            TaskDependency(
                source_node_id=finalize_dependency,
                target_node_id=finalize_node.node_id,
                condition="success",
            )
        )
        graph = TaskGraph(
            goal=task_request.goal,
            summary="Deterministic file read and write plan",
            nodes=nodes,
            dependencies=dependencies,
        )
        graph.validate()
        return PlanResult(
            summary="使用文件 Skill 直接读取源文件并写入目标文件",
            graph=graph,
            reasoning=(
                "The request explicitly names a source file to read and exact "
                "content to write to a target file."
            ),
            risk_level="low",
            required_capabilities=["filesystem.read", "filesystem.write"],
        )

    def _file_write_then_read_plan(
        self,
        task_request: TaskRequest,
        action_node: TaskNode,
        path: str,
    ) -> PlanResult:
        read_node = TaskNode(
            node_id="node_read_file",
            type=TaskNodeType.ACTION,
            goal=f"Read {path} after writing it",
            dependencies=[action_node.node_id],
            skill_name="builtin.file.basic",
            input_payload={
                "action": "read",
                "path": path,
            },
        )
        finalize_node = TaskNode(
            node_id="node_finalize",
            type=TaskNodeType.FINALIZE,
            goal="Summarize execution result",
            dependencies=[read_node.node_id],
        )
        graph = TaskGraph(
            goal=task_request.goal,
            summary="Deterministic file write and readback plan",
            nodes=[action_node, read_node, finalize_node],
            dependencies=[
                TaskDependency(
                    source_node_id=action_node.node_id,
                    target_node_id=read_node.node_id,
                    condition="success",
                ),
                TaskDependency(
                    source_node_id=read_node.node_id,
                    target_node_id=finalize_node.node_id,
                    condition="success",
                ),
            ],
        )
        graph.validate()
        return PlanResult(
            summary="使用文件 Skill 直接写入并读回文件",
            graph=graph,
            reasoning=(
                "The request explicitly names the target file, exact file content, "
                "and a read-back verification step."
            ),
            risk_level="low",
            required_capabilities=["filesystem.write", "filesystem.read"],
        )

    def _build_csv_report_plan(
        self,
        task_request: TaskRequest,
        csv_plan: tuple[str, str, str],
    ) -> PlanResult:
        path, output_path, column = csv_plan
        node = TaskNode(
            node_id="node_csv_report",
            type=TaskNodeType.ACTION,
            goal=f"Sum {column} from {path} and write {output_path}",
            skill_name="builtin.data.csv",
            input_payload={
                "action": "report",
                "path": path,
                "metrics": [
                    {
                        "name": f"total_{column}",
                        "op": "sum",
                        "column": column,
                    }
                ],
                "output_path": output_path,
            },
        )
        return self._single_action_plan(
            task_request,
            node,
            summary="使用 CSV Skill 直接生成汇总报告",
            graph_summary="Deterministic CSV report plan",
            reasoning="The request explicitly asks to sum one CSV column and write a report.",
            required_capabilities=["filesystem.read", "filesystem.write"],
        )

    def _single_action_plan(
        self,
        task_request: TaskRequest,
        action_node: TaskNode,
        *,
        summary: str,
        graph_summary: str,
        reasoning: str,
        required_capabilities: list[str],
    ) -> PlanResult:
        finalize_node = TaskNode(
            node_id="node_finalize",
            type=TaskNodeType.FINALIZE,
            goal="Summarize execution result",
            dependencies=[action_node.node_id],
        )
        graph = TaskGraph(
            goal=task_request.goal,
            summary=graph_summary,
            nodes=[action_node, finalize_node],
            dependencies=[
                TaskDependency(
                    source_node_id=action_node.node_id,
                    target_node_id=finalize_node.node_id,
                    condition="success",
                )
            ],
        )
        graph.validate()
        return PlanResult(
            summary=summary,
            graph=graph,
            reasoning=reasoning,
            risk_level="low",
            required_capabilities=required_capabilities,
        )

    @staticmethod
    def _direct_csv_report_plan(task_request: TaskRequest) -> tuple[str, str, str] | None:
        if (
            task_request.skill_name != "builtin.data.csv"
            and "builtin.data.csv" not in task_request.preferred_skills
        ):
            return None
        goal = task_request.goal
        path_match = re.search(
            r"\b(?:from|in)\s+([A-Za-z0-9_./-]+\.csv)\b",
            goal,
            re.IGNORECASE,
        )
        output_match = re.search(
            r"\bwrite\s+([A-Za-z0-9_./-]+\.(?:md|txt|csv))\b",
            goal,
            re.IGNORECASE,
        )
        column_match = re.search(
            r"\bfrom\s+the\s+([A-Za-z0-9_.-]+)\s+column\b",
            goal,
            re.IGNORECASE,
        )
        if column_match is None:
            column_match = re.search(
                r"\b(?:sum|total)\s+(?:of\s+)?([A-Za-z0-9_.-]+)\b",
                goal,
                re.IGNORECASE,
            )
        if path_match is None or output_match is None or column_match is None:
            return None
        column = column_match.group(1).strip().lower()
        if not column:
            return None
        return path_match.group(1), output_match.group(1), column

    @staticmethod
    def _parse_json_report_goal(
        goal: str,
    ) -> tuple[str, str, list[dict[str, str]]] | None:
        path_match = re.search(
            r"\b(?:from|in|read)\s+([A-Za-z0-9_./-]+\.json)\b",
            goal,
            re.IGNORECASE,
        )
        output_match = re.search(
            r"\bwrite\s+([A-Za-z0-9_./-]+\.(?:md|txt|json))\b",
            goal,
            re.IGNORECASE,
        )
        if path_match is None or output_match is None:
            return None

        path = path_match.group(1)
        extract_match = re.search(
            rf"\bextract\s+(.+?)\s+from\s+{re.escape(path)}\b",
            goal,
            re.IGNORECASE,
        )
        if extract_match is None:
            extract_match = re.search(
                r"\bextract\s+(.+?)(?:\.\s+Write\b|\s+and\s+write\b)",
                goal,
                re.IGNORECASE,
            )
        if extract_match is None:
            return None

        fields: list[dict[str, str]] = []
        for item in re.split(r",\s*(?:and\s+)?|\s+and\s+", extract_match.group(1)):
            query = item.strip().strip("\"'")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+", query):
                continue
            fields.append({"label": query.rsplit(".", 1)[-1], "query": query})
        if not fields:
            return None
        return path, output_match.group(1), fields

    @staticmethod
    def _direct_replace_line_plan(
        goal: str, *, fallback_path: str | None = None
    ) -> tuple[str, str, str] | None:
        path_match = re.search(
            r"\b(?:in|file)\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+)",
            goal,
            flags=re.IGNORECASE,
        )
        content_match = re.search(
            r"\bso\s+it\s+says\s+(.+?)(?:\.|$)",
            goal,
            flags=re.IGNORECASE,
        )
        if content_match is None:
            content_match = re.search(
                r"\b(?:to|with)\s+([A-Za-z0-9_.-]+\s*:\s*.+?)(?:\.|$)",
                goal,
                flags=re.IGNORECASE,
            )
        prefix_match = re.search(
            r"\bonly\s+the\s+([A-Za-z0-9_.-]+)\s+line\b",
            goal,
            flags=re.IGNORECASE,
        )
        if content_match is None:
            return None
        path = path_match.group(1) if path_match is not None else fallback_path
        if not path:
            return None
        content = content_match.group(1).strip().strip("\"'")
        if not content or "\n" in content:
            return None
        if prefix_match is not None:
            line_prefix = f"{prefix_match.group(1).strip()}:"
        elif ":" in content:
            line_prefix = content.split(":", 1)[0].strip() + ":"
        else:
            return None
        if line_prefix == ":":
            return None
        return path, line_prefix, content

    @staticmethod
    def _direct_replace_and_append_plan(
        goal: str, *, fallback_path: str | None = None
    ) -> tuple[str, str, str, str] | None:
        path_match = re.search(
            r"\b(?:in|file)\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+)",
            goal,
            flags=re.IGNORECASE,
        )
        path = path_match.group(1) if path_match is not None else fallback_path
        if not path:
            return None
        match = re.search(
            r"\bchange\s+only\s+([A-Za-z0-9_.-]+)\s+to\s+"
            r"([A-Za-z0-9_.-]+\s*:\s*[^.]+?)\s+and\s+append\s+"
            r"([A-Za-z0-9_.-]+\s*:\s*[^.]+?)(?:\.|$)",
            goal,
            flags=re.IGNORECASE,
        )
        if match is None:
            return None
        line_prefix = f"{match.group(1).strip()}:"
        replacement = match.group(2).strip().strip("\"'")
        append_content = match.group(3).strip().strip("\"'")
        if not replacement or not append_content:
            return None
        return path, line_prefix, replacement, append_content

    @staticmethod
    def _direct_file_write_plan(goal: str) -> tuple[str, str] | None:
        if re.search(r"\bappend\b|追加", goal, flags=re.IGNORECASE):
            return None
        path_match = re.search(
            r"\b(?:create|write)\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+)\b",
            goal,
            flags=re.IGNORECASE,
        )
        one_line_match = re.search(
            r"\bwith\s+exactly\s+one\s+line:\s*(.+?)"
            r"(?:(?<=\.)\s+(?:Then|Do|Keep|If|Use)\b|$)",
            goal,
            flags=re.IGNORECASE,
        )
        if path_match is None:
            return None
        if one_line_match is not None:
            content = one_line_match.group(1).strip().strip("\"'")
            if not content or "\n" in content:
                return None
            return path_match.group(1), content + "\n"
        lines_match = re.search(
            r"\b(?:file\s+must\s+contain|with)\s+(?:these\s+)?"
            r"(?:(?:exactly\s+)?these\s+|(?:exactly\s+)?(?:two|three|four|five|\d+)\s+)"
            r"lines:\s+"
            r"(.+?)(?:\.\s+(?:Then|Do|Keep|If)\b|\.?$)",
            goal,
            flags=re.IGNORECASE,
        )
        if lines_match is None:
            return None
        raw_lines = [
            item.strip().strip("\"'")
            for item in re.split(r",\s*(?:and\s+)?|\s+and\s+", lines_match.group(1))
            if item.strip()
        ]
        if len(raw_lines) < 2 or any("\n" in item for item in raw_lines):
            return None
        return path_match.group(1), "\n".join(raw_lines) + "\n"

    @staticmethod
    def _direct_file_field_report_plan(goal: str) -> tuple[str, str] | None:
        path_match = re.search(
            r"\b(?:into|write|create)\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+)\b",
            goal,
            flags=re.IGNORECASE,
        )
        fields_match = re.search(
            r"\bwith\s+(.+?)(?:\.|$)",
            goal,
            flags=re.IGNORECASE,
        )
        if fields_match is None:
            fields_match = re.search(
                r"\b(?:report|file)\s+must\s+contain\s+(.+?)"
                r"(?:\.\s+(?:Do|Then|Keep|If)\b|\.?$)",
                goal,
                flags=re.IGNORECASE,
            )
        if path_match is None or fields_match is None:
            return None
        if re.search(r"\bexactly\s+(?:these\s+)?(?:two|three|four|five|\d+)?\s*lines\b", goal, re.I):
            return None
        fields: list[str] = []
        for item in re.split(r",\s*(?:and\s+)?|\s+and\s+", fields_match.group(1)):
            field = item.strip().strip("\"'").rstrip(".").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\s*:\s*[^,\n]+", field):
                continue
            fields.append(field)
        if len(fields) < 2:
            return None
        return path_match.group(1), "\n".join(fields) + "\n"

    @staticmethod
    def _direct_file_append_plan(
        goal: str, *, fallback_path: str | None = None
    ) -> tuple[str, str] | None:
        if re.search(
            r"\b(?:change|update|replace)\s+only\b|\breplace\b|改成|替换",
            goal,
            flags=re.IGNORECASE,
        ):
            return None
        content_match = re.search(
            r"\bappend\s+(?:a\s+)?(?:second|third|new|another)?\s*"
            r"line(?:\s+(?:to|in)\s+(?:that|the)?\s*(?:same\s+)?file)?\s*:\s*(.+?)"
            r"(?:(?<=\.)\s+(?:Then|Do|Keep|If|Use)\b|$)",
            goal,
            flags=re.IGNORECASE,
        )
        if content_match is None:
            content_match = re.search(
                r"\badd\s+(?:a\s+)?(?:[A-Za-z0-9_.-]+\s+)?line\s+to\s+"
                r"(?:that|the)(?:\s+same)?\s+(?:plan|file)\s*:\s*(.+?)"
                r"(?:(?<=\.)\s+(?:Then|Do|Keep|If|Use)\b|$)",
                goal,
                flags=re.IGNORECASE,
            )
        if content_match is None:
            content_match = re.search(
                r"追加(?:一|二|第二|新)?行[:：]\s*(.+?)(?:。|$)",
                goal,
                flags=re.IGNORECASE,
            )
        if content_match is None:
            return None
        path_match = re.search(
            r"\b(?:in|to|file|create|write)\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+)",
            goal,
            flags=re.IGNORECASE,
        )
        path = path_match.group(1) if path_match is not None else fallback_path
        if not path:
            return None
        content = content_match.group(1).strip().strip("\"'")
        if not content or "\n" in content:
            return None
        return path, content + "\n"

    @staticmethod
    def _requires_file_readback(goal: str) -> bool:
        return bool(
            re.search(
                r"\b(?:read\s+(?:the\s+)?file\s+back|read\s+it\s+back|read\s+back)\b"
                r"|读回|回读|回显",
                goal,
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _source_read_path(goal: str, *, target_path: str) -> str | None:
        match = re.search(
            r"\bread\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+)\b",
            goal,
            flags=re.IGNORECASE,
        )
        if match is None:
            return None
        source_path = match.group(1)
        if source_path == target_path:
            return None
        return source_path

    @staticmethod
    def _recent_context_file_path(
        goal: str,
        recent_task_artifacts: list[dict[str, Any]] | None,
    ) -> str | None:
        if not recent_task_artifacts:
            return None
        if not re.search(
            r"\b(?:same|that|previous|it|plan|file|first|second)\b|同一个|那个|这个|第一个|第二个",
            goal,
            re.I,
        ):
            return None
        paths = Planner._recent_context_file_paths(recent_task_artifacts)
        if not paths:
            return None
        if re.search(r"\bfirst\b|第一个", goal, re.I):
            return paths[0]
        if re.search(r"\bsecond\b|第二个", goal, re.I) and len(paths) >= 2:
            return paths[1]
        return paths[-1]

    @staticmethod
    def _recent_context_file_paths(
        recent_task_artifacts: list[dict[str, Any]],
    ) -> list[str]:
        path_pattern = re.compile(
            r"\b((?:reports|plans|notes|configs|inputs|briefs|ops|docs|outputs)/"
            r"[A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+)\b"
        )

        def iter_texts(entry: dict[str, Any]):
            for key in ("plan_summary", "summary", "goal"):
                value = entry.get(key)
                if isinstance(value, str):
                    yield value
            for node in entry.get("nodes", []) or []:
                if not isinstance(node, dict):
                    continue
                value = node.get("goal")
                if isinstance(value, str):
                    yield value
                for artifact in node.get("artifacts", []) or []:
                    if not isinstance(artifact, dict):
                        continue
                    for key in ("path", "description"):
                        value = artifact.get(key)
                        if isinstance(value, str):
                            yield value

        ordered_entries = sorted(
            (entry for entry in recent_task_artifacts if isinstance(entry, dict)),
            key=lambda entry: str(entry.get("created_at") or ""),
        )
        paths: list[str] = []
        seen: set[str] = set()
        for entry in ordered_entries:
            if not isinstance(entry, dict):
                continue
            for text in iter_texts(entry):
                match = path_pattern.search(text)
                if match:
                    path = match.group(1)
                    if path not in seen:
                        seen.add(path)
                        paths.append(path)
        return paths

    @staticmethod
    def _parse_config_report_goal(goal: str) -> tuple[str, str, list[str]] | None:
        path_match = re.search(
            r"\b(?:read|from)\s+([A-Za-z0-9_./-]+\.(?:toml|ya?ml|xml|json))\b",
            goal,
            flags=re.IGNORECASE,
        )
        output_match = re.search(
            r"\bwrite\s+([A-Za-z0-9_./-]+\.(?:md|txt|csv))\b",
            goal,
            flags=re.IGNORECASE,
        )
        fields_match = re.search(
            r"\bcontaining\s+(.+?)(?:\.\s+If\b|\.?$)",
            goal,
            flags=re.IGNORECASE,
        )
        if path_match is None or output_match is None or fields_match is None:
            return None
        labels: list[str] = []
        for raw in re.split(r",|\band\b", fields_match.group(1), flags=re.IGNORECASE):
            if ":" not in raw:
                continue
            label = raw.split(":", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]+", label):
                labels.append(label)
        if not labels:
            return None
        deduped = list(dict.fromkeys(labels))
        return path_match.group(1), output_match.group(1), deduped

    @staticmethod
    def _config_query_for_label(label: str) -> str:
        aliases = {
            "service_name": "service.name",
            "retries": "retries",
        }
        return aliases.get(label, label)

    def replan(
        self,
        *,
        original_request: TaskRequest,
        replan_goal: str,
        bootstrap: BootstrapContext,
        workflow_matches: list[dict[str, Any]],
        vector_matches: list[dict[str, Any]],
        skill_registry_entries: list[dict[str, Any]],
    ) -> tuple[PlanResult, Any]:
        request = TaskRequest(
            goal=replan_goal,
            expected_output=original_request.expected_output,
            payload=dict(original_request.payload),
            executor=original_request.executor,
            skill_name=original_request.skill_name,
            mode=original_request.mode,
            source=original_request.source,
            context_refs=list(original_request.context_refs),
            constraints=original_request.constraints,
            preferred_skills=list(original_request.preferred_skills),
            metadata={**original_request.metadata, "phase": "replan"},
            execution_mode=original_request.execution_mode,
        )
        return self.plan(
            task_request=request,
            bootstrap=bootstrap,
            memory_summaries=[],
            workflow_matches=workflow_matches,
            vector_matches=vector_matches,
            failure_patterns=[],
            graph_workflow_matches=[],
            graph_skill_matches=[],
            graph_failure_patterns=[],
            recall_explanations=[],
            skill_registry_entries=skill_registry_entries,
        )

    def _serialize_task_request(self, task_request: TaskRequest) -> dict[str, Any]:
        return {
            "task_id": task_request.task_id,
            "goal": task_request.goal,
            "expected_output": task_request.expected_output,
            "payload": dict(task_request.payload),
            "executor": task_request.executor,
            "skill_name": task_request.skill_name,
            "mode": str(task_request.mode),
            "source": str(task_request.source),
            "context_refs": list(task_request.context_refs),
            "preferred_skills": list(task_request.preferred_skills),
            "metadata": dict(task_request.metadata),
            "execution_mode": task_request.execution_mode,
            "constraints": {
                "allow_network": task_request.constraints.allow_network,
                "allow_delete": task_request.constraints.allow_delete,
                "allow_memory_read": task_request.constraints.allow_memory_read,
                "timeout_seconds": task_request.constraints.timeout_seconds,
                "max_permission": str(task_request.constraints.max_permission),
            },
        }
