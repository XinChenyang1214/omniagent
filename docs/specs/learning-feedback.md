# Learning Feedback Spec

Last reviewed: 2026-07-31

## 文档状态

- Document type: spec
- Workstream: `META-010` / Learning feedback
- Document status: active
- Implementation status: verified
- Authority scope: 任务结束后向用户展示 learning loop 状态的最小数据契约和输出规则
- Completion evidence: [`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](../workstreams/completed/AGENT_USABILITY_LEARNING_LOOP_MVP.md)

## 目标

Learning feedback 让用户在任务结束时直接看懂 Agent 是否学到了东西、是否会复用、还需要用户做什么。

它是现有 artifacts 的派生视图，不创建新的学习状态机：

```text
ExperienceRecord + procedure_match + habit_fast_path + procedure memory
  -> learning-feedback/v1
  -> CLI / chat task ending reply
  -> real-world eval evidence index / dashboard
```

## 输出时机

每次 `run` / chat task 结束后，Gateway 必须生成 feedback 并附加到 `TaskExecutionReport`：

```text
workspace/tasks/<task_id>/learning/feedback.json
```

CLI JSON 输出必须包含 `learning_feedback` 字段。

CLI 非 JSON 和 chat 回复必须渲染用户可读的 `学习反馈` 区块。

## Schema

Required top-level fields:

| Field | Meaning |
|---|---|
| `schema_version` | `learning-feedback/v1` |
| `task_id` | source task id |
| `task_goal` | user-facing task goal |
| `experience` | experience record status, validation verdict, evidence refs and learnable flag |
| `procedure` | matched / generated procedure or habit summary |
| `habit_fast_path` | fast path decision, skipped stages and policy gate |
| `reuse` | whether the result can be reused now, why, and what happens next time |
| `review` | whether user action is required and concrete governance commands |
| `summary_lines` | short deterministic lines rendered in task-ending replies |

## 输出规则

- 必须说明本次是否写入 experience，以及 validation verdict。
- 必须说明本次是否生成 procedure draft，或命中已有 procedure / habit。
- 必须说明为什么当前可以或不可以复用。
- 必须说明下一次类似任务会怎样变快或更稳定。
- 如果生成 draft，必须提示 review 命令。
- 如果命中 procedure / habit，必须提示 show / history / explain-match / edit / disable 入口。
- 如果没有 passed validation evidence，必须明确不会自动升级学习结果。
- feedback 不能改变 procedure / habit / skill 生命周期，只能读取并解释已有 evidence。

## 验收矩阵

| Requirement | Evidence needed | Status |
|---|---|---|
| 任务报告包含结构化 feedback | `tests/test_learning_feedback.py::LearningFeedbackIntegrationTest::test_first_run_feedback_explains_draft_and_review_action`；`TaskExecutionReport.learning_feedback` 和 `tasks/<task_id>/learning/feedback.json` 存在 | implemented |
| CLI 非 JSON 输出可见学习状态 | `tests/test_learning_feedback.py::LearningFeedbackIntegrationTest::test_cli_plain_output_includes_learning_feedback`；输出包含 `学习反馈`、经验记录和 review 提示 | implemented |
| Habit fast path 可解释复用收益和修正入口 | `tests/test_learning_feedback.py::LearningFeedbackIntegrationTest::test_habit_fast_path_feedback_explains_reuse_and_correction_commands`；feedback 说明命中 habit、fast path accepted、可禁用或修正 | implemented |
| Chat responder 不遗漏 learning feedback | `tests/test_responder_validation.py::ResponderIntegrationTest::test_learning_feedback_injected_and_appended_to_reply`；Responder payload 包含 feedback，最终回复追加 `学习反馈` 区块 | implemented |
| 连续任务中能验证生成、复用、解释、修正和治理学习结果 | `evals/real_world/scenarios.json::learning_loop_feedback_sequence`；`scripts/run_real_world_evals.py --scenario learning_loop_feedback_sequence` 验证 draft -> approved procedure -> verified reuse -> habit fast path -> explain-match -> edit -> disable | verified |
| 真实评测证据进入 evidence index / dashboard | `tests/test_real_world_eval_diagnostics.py` 覆盖 `learning_feedbacks` evidence kind、query filter 和 `learning_feedback_cases` dashboard 视图；`--build-evidence-index` / `--query-evidence-index --evidence-learning-kind learning_feedbacks` / `--evidence-dashboard` 可消费该证据 | verified |

## 非目标

- 不新增面向最终用户的独立产品 dashboard；本阶段只扩展真实评测 evidence dashboard。
- 不新增 skill 类型。
- 不扩大非 learning-loop 能力矩阵；本阶段只新增聚焦 learning feedback 的连续任务场景。
- 不绕过已有 review、permission、validator 或 promotion gate。
