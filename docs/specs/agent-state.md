# Agent State Spec

Last reviewed: 2026-07-31

## 文档状态

- Document type: spec
- Workstream: `META-010`
- Document status: active
- Implementation status: implemented
- Authority scope: Agent state 的最小数据契约、存储边界和验收标准
- Completion evidence: [`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](../workstreams/completed/AGENT_USABILITY_LEARNING_LOOP_MVP.md)

## 目标

Agent state 让 OmniAgent 不再只响应单条 task，而是维护“我是谁、用户偏好是什么、当前项目处在什么状态、现在正在推进什么目标”。

首期只实现 workspace-local、可读、可审计的状态，不做跨设备同步、不做远端 profile、不做自动人格生成。

## 状态对象

### `AgentProfile`

Agent 自身的稳定边界。

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | state schema version |
| `agent_name` | string | Agent 名称 |
| `role` | string | 当前项目中的角色定位 |
| `default_language` | string | 默认沟通语言 |
| `communication_style` | string | 默认输出风格 |
| `capability_boundaries` | list[string] | 明确会做和不会做的边界 |
| `risk_policy_summary` | string | 高风险动作默认策略摘要 |
| `updated_at` | string | ISO timestamp |

### `UserPreference`

用户长期偏好和禁忌。

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | state schema version |
| `preferences` | list[object] | 偏好条目 |
| `disabled_preferences` | list[string] | 被用户禁用的偏好 id |
| `updated_at` | string | ISO timestamp |

Preference item:

| Field | Type | Meaning |
|---|---|---|
| `id` | string | stable id |
| `scope` | string | `global` / `project` / `task_type` |
| `statement` | string | 偏好内容 |
| `source` | string | `explicit_user` / `observed` / `imported` |
| `confidence` | number | 0.0-1.0 |
| `evidence_refs` | list[string] | 支撑该偏好的 evidence path |

### `ProjectState`

当前项目事实和活跃文档状态。

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | state schema version |
| `project_root` | string | 项目根目录 |
| `current_workstream` | string | 当前 workstream 标识；没有 active workstream 时为 `none` 或 `unknown` |
| `authoritative_docs` | list[string] | 当前 active / confirmed 文档路径 |
| `archived_docs` | list[string] | 归档历史文档入口 |
| `open_questions` | list[string] | 当前开放问题 |
| `updated_at` | string | ISO timestamp |

### `CurrentGoal`

当前正在推进的用户目标。

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | state schema version |
| `goal_id` | string | stable id |
| `title` | string | 目标名称 |
| `definition_of_done` | list[string] | 完成标准 |
| `status` | string | `active` / `blocked` / `completed` / `paused` |
| `active_phase` | string | 当前所处阶段 |
| `blockers` | list[string] | 阻塞项 |
| `next_actions` | list[string] | 下一步动作 |
| `evidence_refs` | list[string] | 支撑当前状态的证据 |
| `updated_at` | string | ISO timestamp |

## 存储规则

首期使用 workspace-local JSON 文件，默认位置：

```text
workspace/agent_state/
  agent_profile.json
  user_preferences.json
  project_state.json
  current_goal.json
```

Rules:

- 文件必须是 UTF-8 JSON。
- 写入必须原子化，避免中途崩溃留下半文件。
- 不写入 token、cookie、API key、私钥或真实密码。
- state 中的事实必须能追溯到用户输入、项目文档、代码、评测产物或任务 artifact。
- 用户显式修改的偏好优先级高于模型推断。

## Runtime 使用规则

任务开始前，runtime 应读取：

1. `AgentProfile`
2. `UserPreference`
3. `ProjectState`
4. `CurrentGoal`

注入上下文时只注入摘要，不把完整 state 无限制塞进 prompt。摘要应包含：

- 当前主线
- 当前目标
- 明确偏好
- 高风险边界
- 与本任务相关的开放问题

## CLI / REPL 要求

首期至少需要可查看入口：

```text
omniagent state show
omniagent state show --section profile
omniagent state show --section preferences
omniagent state show --section project
omniagent state show --section goal
```

编辑入口可以后置，但必须保留后续扩展位置。

## 验收矩阵

| Requirement | Evidence needed | Status |
|---|---|---|
| 可初始化默认 Agent state | `tests/test_agent_state.py::AgentStateStoreTest::test_initializes_default_state_files`；首次 workspace bootstrap 后存在四个 JSON 文件 | implemented |
| 可读取并摘要注入任务上下文 | `tests/test_agent_state.py::AgentStateRuntimeIntegrationTest::test_runtime_context_snapshot_contains_agent_state_summary`；`workspace/tasks/<task_id>/context_snapshot.json` 包含 `agent_state_summary` | implemented |
| 可更新 `CurrentGoal` | `tests/test_agent_state.py::AgentStateStoreTest::test_updates_current_goal`；`omniagent state show --section goal --json` 可查看更新结果 | implemented |
| 用户偏好可禁用 | `tests/test_agent_state.py::AgentStateStoreTest::test_summary_excludes_disabled_preferences`；禁用偏好不进入 context summary | implemented |
| 不写入敏感值 | `tests/test_agent_state.py::AgentStateStoreTest::test_rejects_sensitive_state_values`；敏感 key/value 拒绝写入 | implemented |
| 高风险边界进入决策上下文 | `tests/test_agent_state.py::AgentStateRuntimeIntegrationTest::test_runtime_context_snapshot_contains_agent_state_summary`；`tests/test_allow_delete.py::GatewaySubmitDeleteEnforcementTest` 验证删除仍走 approval / permission gate | implemented |

## 实现证据

- Schema / storage: `src/omniagent/agent_state.py`
- Runtime integration: `src/omniagent/runtime.py` 读取 state summary；`src/omniagent/workspace.py` 写入 `context_snapshot.json` 和 `AGENT_STATE.md`
- CLI / REPL: `omniagent state show`；`chat repl` 内 `/state [profile|preferences|project|goal]`
- Planner context: `agent_state_summary` 进入 planner request payload；`AGENT_STATE.md` 进入 bootstrap documents
- Policy evidence: `workspace/tasks/<task_id>/plan/policy_decision.json` 写入 `agent_state_summary`

## 非目标

- 不实现多用户 profile。
- 不实现云同步。
- 不让 LLM 自动覆盖用户显式偏好。
- 不把完整 state 原样注入每次 LLM prompt。
