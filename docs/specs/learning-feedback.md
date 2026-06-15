# Learning Feedback Spec

Last reviewed: 2026-06-15

## 文档状态

- Document type: spec
- Workstream: `META-010` / Learning feedback
- Document status: confirmed
- Implementation status: implementing
- Authority scope: 任务结束后向用户展示 learning loop 状态的最小数据契约和输出规则
- Parent workstream: [`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](../workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md)

## 目标

Learning feedback 让用户在任务结束时直接看懂 Agent 是否学到了东西、是否会复用、还需要用户做什么。

它是现有 artifacts 的派生视图，不创建新的学习状态机：

```text
ExperienceRecord + procedure_match + habit_fast_path + procedure memory
  -> learning-feedback/v1
  -> CLI / chat task ending reply
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

## 非目标

- 不新增 dashboard。
- 不新增 skill 类型。
- 不扩大真实评测矩阵。
- 不绕过已有 review、permission、validator 或 promotion gate。
