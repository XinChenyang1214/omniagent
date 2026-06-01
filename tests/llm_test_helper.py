"""Minimal LLM test-double for integration tests.

TestLLMAdapter returns structurally valid responses matching the expected
schemas without simulating real intelligence.  It does NOT import or
depend on the deleted MockLLMAdapter.

Register it in factory via provider="test".
"""

from __future__ import annotations

import uuid
from typing import Any

from omniagent.config import LLMConfig
from omniagent.llm.base import LLMAdapter
from omniagent.models import LLMResponse, utc_now


class StubLLMAdapter(LLMAdapter):
    """Structural test-double that returns minimal valid JSON for each prompt_name."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig(provider="test", model="test-model")
        # ISSUE-001: expose a call counter + latency knob so tests can
        # verify the adapter→workspace→llm_stats pipeline end-to-end
        # without monkeypatching a real HTTP client.
        self.call_count = 0
        self.fake_latency_ms = 42.0

    def generate_structured(
        self,
        *,
        prompt_name: str,
        system_prompt: str,
        request_payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> LLMResponse:
        request_id = f"test_{uuid.uuid4().hex[:8]}"
        if prompt_name == "planner_v1":
            structured = self._plan(request_payload)
        elif prompt_name == "critic_v1":
            structured = self._critic(request_payload)
        elif prompt_name == "router_v1":
            structured = self._route(request_payload)
        elif prompt_name == "skill_generator_v1":
            structured = self._skill_gen(request_payload)
        elif prompt_name == "responder_v1":
            structured = self._respond(request_payload)
        elif prompt_name == "knowledge_query_v1":
            structured = self._knowledge_query(request_payload)
        elif prompt_name == "completion_evaluator_v1":
            structured = self._completion_evaluation(request_payload)
        elif prompt_name == "llm_native_v1":
            # META-004: LLMNativeExecutor calls the adapter directly to
            # generate free-form text. The stub just echoes the goal as a
            # canned response so tests can verify the end-to-end flow.
            goal = str(request_payload.get("goal", "")).strip() or "empty goal"
            structured = {
                "response": f"[stub llm_native response] {goal}",
                "reasoning": "stub responded deterministically for testing",
            }
        elif prompt_name == "health_check":
            structured = {"ok": True}
        else:
            structured = {}
        self.call_count += 1
        # ISSUE-001: populate observability fields so tests that exercise
        # write_llm_call / llm_stats see non-null latency/tier/attempts.
        # A silent `None` here would mask any broken wiring downstream.
        return LLMResponse(
            provider="test",
            model="test-model",
            prompt_name=prompt_name,
            request_id=request_id,
            raw_output="{}",
            structured_output=structured,
            usage={"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            latency_ms=self.fake_latency_ms,
            tier="tier1_json_schema",
            attempts=1,
            started_at=utc_now(),
        )

    def check_health(self) -> dict[str, Any]:
        return {"healthy": True, "provider": "test", "model": "test-model"}

    # ------------------------------------------------------------------
    # Minimal responders — produce valid structure, not intelligent output
    # ------------------------------------------------------------------

    def _plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        tr = payload.get("task_request", {})
        goal = tr.get("goal", "task")
        executor = tr.get("executor", "file")
        skill_name = tr.get("skill_name")
        user_payload = tr.get("payload", {})

        # Infer executor from "auto"
        if executor == "auto":
            if "code" in user_payload:
                executor = "python"
            elif "url" in user_payload:
                executor = "browser"
            else:
                executor = "file"

        # Graph skill matches are handled by the dispatcher's registry
        # via graph_memory.recommend_skills_for_goal(), not by the planner.
        # Do not put them in required_skills or skill_name.

        # Build hint context
        workflow_matches = payload.get("workflow_matches", [])
        vector_matches = payload.get("vector_matches", [])
        graph_workflow_matches = payload.get("graph_workflow_matches", [])
        failure_patterns = payload.get("failure_patterns", [])
        has_recall = bool(workflow_matches or vector_matches or graph_workflow_matches)
        has_failures = bool(failure_patterns or payload.get("graph_failure_patterns", []))

        summary = "将目标拆分为执行计划"
        if has_recall:
            summary += "，并参考历史经验优化计划"
        if has_failures:
            summary += "，规避已知失败路径"

        # Build a sensible default payload if none provided
        effective_payload = dict(user_payload) if user_payload else {}
        if not effective_payload and executor == "file":
            effective_payload = {"action": "list", "path": "."}
        elif not effective_payload and executor == "python":
            effective_payload = {"code": "result = {'status': 'ok'}\nprint('ok')"}
        elif not effective_payload and executor == "browser":
            effective_payload = {"url": "file://./"}

        action_node: dict[str, Any] = {
            "node_id": "node_action_1",
            "type": "action",
            "goal": goal,
            "dependencies": [],
            "required_skills": [skill_name] if skill_name else [],
            "executor_hint": executor if not skill_name else None,
            "skill_name": skill_name,
            "risk_level": "medium",
            "retry_policy": {
                "max_retries": 2,
                "backoff_seconds": 1,
                "degrade_to": [],
                "replan_on_failure": True,
            },
            "input_payload": effective_payload,
            "metadata": {
                "workflow_hint_count": len(workflow_matches) + len(graph_workflow_matches),
                "graph_workflow_hint_count": len(graph_workflow_matches),
                "vector_hint_count": len(vector_matches),
            },
        }
        finalize_node: dict[str, Any] = {
            "node_id": "node_finalize",
            "type": "finalize",
            "goal": "汇总执行结果",
            "dependencies": ["node_action_1"],
            "required_skills": [],
            "executor_hint": None,
            "skill_name": None,
            "risk_level": "low",
            "retry_policy": {
                "max_retries": 0,
                "backoff_seconds": 0,
                "degrade_to": [],
                "replan_on_failure": False,
            },
            "input_payload": {},
            "metadata": {},
        }
        return {
            "summary": summary,
            "reasoning": "test planner 基于目标生成最小执行计划",
            "risk_level": "medium",
            "required_capabilities": [executor] if executor else [],
            "graph": {
                "goal": goal,
                "summary": summary,
                "nodes": [action_node, finalize_node],
                "dependencies": [
                    {
                        "source_node_id": "node_action_1",
                        "target_node_id": "node_finalize",
                        "condition": "on_success",
                    },
                ],
            },
        }

    def _critic(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("execution_result", {})
        target_type = payload.get("target_type", "node")
        target_id = payload.get("target_id", "unknown")
        node = payload.get("node", {})
        retry_policy = node.get("retry_policy", {})
        attempts = node.get("attempts", 0)
        max_retries = retry_policy.get("max_retries", 0)

        if result.get("success"):
            return {
                "target_type": target_type,
                "target_id": target_id,
                "action": "accept",
                "summary": "执行结果通过",
                "rationale": "节点执行成功",
                "confidence": 0.95,
                "suggested_retry_policy": None,
                "downgrade_target": None,
                "replan_goal": None,
            }

        if attempts <= max_retries:
            return {
                "target_type": target_type,
                "target_id": target_id,
                "action": "retry",
                "summary": "建议重试",
                "rationale": "尚未达到最大重试次数",
                "confidence": 0.8,
                "suggested_retry_policy": None,
                "downgrade_target": None,
                "replan_goal": None,
            }

        if retry_policy.get("replan_on_failure"):
            original_goal = node.get("goal", "")
            error_msg = result.get("message", "")
            return {
                "target_type": target_type,
                "target_id": target_id,
                "action": "replan",
                "summary": "建议重规划",
                "rationale": "重试已耗尽，需要新方案",
                "confidence": 0.75,
                "suggested_retry_policy": None,
                "downgrade_target": None,
                "replan_goal": f"重新规划: {original_goal} (失败原因: {error_msg})",
            }

        return {
            "target_type": target_type,
            "target_id": target_id,
            "action": "fail",
            "summary": "建议终止",
            "rationale": "无可用恢复动作",
            "confidence": 0.9,
            "suggested_retry_policy": None,
            "downgrade_target": None,
            "replan_goal": None,
        }

    def _route(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = payload.get("message", "")
        return {
            "input_type": "new_task",
            "task_action": "create",
            "reasoning": "test router 默认路由为新任务",
            "confidence": 0.8,
            "extracted_goal": message if message else None,
            "metadata": {},
        }

    def _respond(self, payload: dict[str, Any]) -> dict[str, Any]:
        fallback = payload.get("fallback_message", "")
        task_summary = payload.get("task_summary", {})
        route_type = payload.get("route_type", "")
        if task_summary:
            status = task_summary.get("status", "")
            msg = task_summary.get("message", "")
            goal = task_summary.get("goal", "")
            if task_summary.get("success"):
                reply = f"已完成: {goal}。{msg}" if goal else msg
            else:
                reply = f"任务失败: {msg}" if msg else f"任务状态: {status}"
        elif route_type == "status_query":
            active = payload.get("active_task", {})
            reply = f"当前任务: {active.get('goal', '无')}，状态: {active.get('status', '无')}"
        elif route_type == "failure_query":
            active = payload.get("active_task", {})
            reply = f"失败原因: {active.get('error_message', '未知')}"
        else:
            reply = fallback or "已收到你的消息。"
        return {"reply": reply, "suggested_actions": []}

    def _knowledge_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Minimal deterministic knowledge-query response for integration tests.

        The stub proves the end-to-end wiring (gateway → LLM adapter →
        reply); it does not exercise real language understanding.
        """
        question = payload.get("question", "")
        snippets = payload.get("document_snippets", []) or []
        sources: list[dict[str, Any]] = []
        for snippet in snippets[:2]:
            if isinstance(snippet, dict) and "document" in snippet:
                sources.append({"document": snippet["document"], "line": snippet.get("line")})
        reply_bits: list[str] = []
        if snippets:
            reply_bits.append(
                "根据工作区文档，我找到了以下相关信息："
                + "；".join(
                    s.get("text", "") for s in snippets[:2] if isinstance(s, dict) and s.get("text")
                )
            )
        else:
            reply_bits.append("目前工作区没有记录这个问题的相关内容。")
        if question:
            reply_bits.append(f"（问题：{question}）")
        return {"reply": " ".join(reply_bits), "sources": sources}

    def _completion_evaluation(self, payload: dict[str, Any]) -> dict[str, Any]:
        task = payload.get("task", {})
        result = payload.get("execution_result", {})
        success = bool(result.get("success"))
        message = str(result.get("message", ""))
        if success:
            return {
                "task_id": str(task.get("task_id", "")),
                "task_satisfied": True,
                "completion_score": 1.0,
                "summary": "任务已满足用户目标",
                "missing_requirements": [],
                "unexpected_side_effects": [],
                "evidence": ["execution_result.success=True", message],
                "primary_failure_layer": "none",
                "model_capability_suspected": False,
                "recommended_next_action": "none",
                "confidence": 0.9,
            }
        return {
            "task_id": str(task.get("task_id", "")),
            "task_satisfied": False,
            "completion_score": 0.0,
            "summary": "任务未满足用户目标",
            "missing_requirements": ["执行结果失败"],
            "unexpected_side_effects": [],
            "evidence": ["execution_result.success=False", message],
            "primary_failure_layer": "tool_or_skill",
            "model_capability_suspected": False,
            "recommended_next_action": "manual_review",
            "confidence": 0.75,
        }

    def _skill_gen(self, payload: dict[str, Any]) -> dict[str, Any]:
        skill_type = payload.get("skill_type", "file")
        # Return None to force fallback to template-based generation,
        # which produces code that actually interacts with the sandbox
        return {
            "main_code": "",
            "test_code": "",
            "reasoning": f"fallback to template for {skill_type}",
        }

    def _skill_gen_real(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Unused — for reference only."""
        skill_type = payload.get("skill_type", "file")
        goal = payload.get("goal", "generated skill")
        return {
            "main_code": (
                "from __future__ import annotations\n\n"
                f"def run(payload: dict, context: dict) -> dict:\n"
                f'    """Auto-generated skill: {goal}"""\n'
                f"    return {{'success': True, 'status': 'ok', 'message': '{goal}', 'output': {{}}}}\n"
            ),
            "test_code": (
                "import unittest\n\n"
                "class FakeSandbox:\n"
                "    def resolve_path(self, path): return f'/fake/{path}'\n"
                "    def list_dir(self, path): return {'entries': []}\n"
                "    def read_text(self, path): return {'content': ''}\n"
                "    def write_text(self, path, content, overwrite=False): return {'written': True, 'path': path}\n"
                "    def run_python(self, code): return {'stdout': '', 'stderr': '', 'returncode': 0}\n"
                "    def fetch_url(self, url): return {'status_code': 200, 'body': '{}', 'headers': {}}\n\n"
                "class TestSkill(unittest.TestCase):\n"
                "    def test_run(self):\n"
                "        from main import run\n"
                "        result = run({}, {'sandbox': FakeSandbox()})\n"
                "        self.assertTrue(result['success'])\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n"
            ),
            "reasoning": f"生成了最小可用的 {skill_type} 类型 Skill",
        }
