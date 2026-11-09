# Task Intake Assessment 规格

Last reviewed: 2026-07-31

## 文档状态

- Document type: behavioral and artifact specification
- Document status: active
- Implementation status: verified
- Schema version: `task-intake/v1`
- Authority scope: 任务执行前的类型、风险、工具、验证与确认预判

## 目标

系统必须在规划或 direct execute 之前留下可审计的任务理解快照，避免把所有输入
默认解释成代码任务，也让用户和评测可以检查 Agent 最初如何理解任务。

Task intake 是确定性启发式判断。它不替代：

- Planner 对复杂任务的规划；
- `ExecutionPolicy` 的路由；
- PermissionManager 的最终授权；
- 用户对高风险动作的确认；
- Critic 和 CompletionEvaluator 的执行后验证。

## 写入时机和位置

每次新任务或恢复执行路径在正式执行前调用 `assess_task_intake()`，并：

- 写入 `workspace/tasks/<task_id>/plan/intake_assessment.json`；
- 发布 `task.intake_assessed` 事件；
- 在 CLI 非 JSON 事件流中显示任务预判；
- 将同一 assessment 带入任务结束摘要和评测 evidence。

## Schema

| 字段 | 类型 | 约束 |
|---|---|---|
| `schema_version` | string | 固定为 `task-intake/v1` |
| `task_id` | string | 当前任务 ID |
| `task_type` | string | 明确 metadata 优先，否则按 executor、skill、payload 和文本信号推断 |
| `task_type_signals` | string[] | 支撑分类的可解释信号 |
| `risk_level` | string | `low`、`medium` 或 `high` |
| `risk_reasons` | string[] | 权限、网络、进程、删除或外部路径等风险原因 |
| `tools` | string[] | 候选 builtin/generated skill；无法推断时为 `planner` |
| `matched_tool_entries` | object[] | registry 中与候选工具匹配的精简信息 |
| `validation_strategy` | string | 预期验证策略 |
| `validation_targets` | string[] | expected output 或目标 artifact |
| `confirmation_required` | boolean | 预判是否需要确认 |
| `confirmation_reasons` | string[] | 需要确认的原因 |
| `source` | string | 请求来源 |
| `executor` | string | 请求中的 executor |
| `skill_name` | string/null | 显式 skill |

## 分类与风险规则

当前内置任务类型包括：

- `file_organization`
- `information_synthesis`
- `document_generation`
- `data_processing`
- `web_information`
- `task_planning`
- `light_automation`
- `development_maintenance`
- `general_personal_task`

显式 `metadata.task_type` 优先于启发式推断。删除或外部路径属于高风险；L4、
进程/代码执行、网络/browser 工具至少提升风险并触发相应确认预判。最终是否执行
仍由权限和 policy gate 决定。

## 验证策略

策略名称必须描述可检查对象，例如 artifact 文件、数据结果、内容召回或测试结果。
如果请求提供 `expected_output`，它应进入 validation targets。Task intake 只声明
预期验证，不得伪造已完成验证。

## 验收

- `tests/test_task_intake.py` 覆盖文件、网络、数据、HTML 和 CLI 可见性；
- `tests/test_real_world_eval_diagnostics.py` 覆盖 assessment 收集和场景断言；
- `general_agent_usability_*` 场景定义必须声明 task type、risk level、tool、
  validation strategy 和 confirmation policy。
