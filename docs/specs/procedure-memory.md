# Procedure Memory Spec

Last reviewed: 2026-07-31

## 文档状态

- Document type: spec
- Workstream: `META-010`
- Document status: active
- Implementation status: implemented
- Authority scope: procedure / verified procedure / habit 的生命周期、存储、检索和升级规则
- Completion evidence: [`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](../workstreams/completed/AGENT_USABILITY_LEARNING_LOOP_MVP.md)

Scope note: 本 spec 覆盖 procedure / habit 生命周期和 fast path；habit -> skill promotion 的实现归 Phase 5。

## 目标

Procedure memory 让 Agent 从经验中形成可复用流程，而不是每次遇到相似任务都重新完整规划。

核心链路：

```text
ExperienceRecord
  -> ProcedureDraft
  -> VerifiedProcedure
  -> Habit
  -> Skill promotion candidate
```

Procedure memory 是学习闭环的第一层可见成果。它必须可审计、可禁用、可降级，不能绕过权限和验证。

## 生命周期状态

| State | Meaning | Can auto-run? |
|---|---|---|
| `draft` | 从 experience 提炼出的候选流程 | no |
| `verified` | 通过测试、artifact 或用户确认验证的流程 | low-risk only, with validator |
| `habit` | 多次验证成功且低回归率的稳定流程 | low-risk yes; medium/high risk requires confirmation |
| `disabled` | 用户或系统禁用 | no |
| `deprecated` | 环境变化或失败后不再推荐 | no |

状态转换：

```text
draft -> verified -> habit
draft -> disabled
verified -> draft
verified -> disabled
habit -> verified
habit -> disabled
habit -> skill_promotion_candidate
```

## Procedure Schema

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | schema version |
| `procedure_id` | string | stable id |
| `name` | string | human-readable name |
| `status` | string | lifecycle state |
| `trigger` | object | when this procedure applies |
| `risk_level` | string | `low` / `medium` / `high` |
| `required_permissions` | list[string] | permission requirements |
| `inputs` | list[object] | required input fields |
| `steps` | list[object] | ordered steps |
| `outputs` | list[object] | expected outputs |
| `validator` | object | validation method and evidence requirements |
| `source_experience_ids` | list[string] | origin experience records |
| `success_count` | integer | verified successes |
| `failure_count` | integer | verified failures |
| `confidence` | number | 0.0-1.0 |
| `updated_at` | string | ISO timestamp |

Trigger object:

| Field | Type | Meaning |
|---|---|---|
| `task_patterns` | list[string] | natural-language or structured task patterns |
| `required_context` | list[string] | context required before matching |
| `negative_patterns` | list[string] | conditions that should prevent matching |

Step object:

| Field | Type | Meaning |
|---|---|---|
| `step_id` | string | stable step id |
| `action_type` | string | `tool` / `skill` / `executor` / `human_confirm` / `validator` |
| `target` | string | tool, skill, executor, or validation target |
| `input_template` | object | input shape or template |
| `expected_output` | string | expected result summary |
| `fallback` | string | what to do when step fails |

Validator object:

| Field | Type | Meaning |
|---|---|---|
| `method` | string | `test` / `artifact_check` / `manual_review` / `completion_eval` |
| `required_evidence` | list[string] | evidence refs or artifact types |
| `failure_behavior` | string | `downgrade` / `disable` / `request_review` |

## 存储规则

Default workspace-local storage:

```text
workspace/procedures/
  drafts/
  verified/
  habits/
  disabled/
  index.jsonl
```

Rules:

- 每个 procedure 一个 JSON 文件。
- `index.jsonl` 只保存匹配所需摘要。
- 文件名使用 `procedure_id`，不要依赖 display name。
- 禁用和降级必须保留原始 evidence lineage。
- 用户编辑后必须更新 `updated_at` 并保留 audit note。

## Review / Edit Rules

用户必须能修正不合适的学习内容，但修正不能绕过验证门槛。

Edit rules:

- 只允许受控修改 `name`、`trigger`、`risk_level`、`required_permissions`、`inputs`、`steps`、`outputs` 和 `validator`。
- 不允许直接编辑 `status`、`success_count`、`failure_count`、`source_experience_ids` 等 promotion gate 字段。
- 只改 `name` 不改变执行资格。
- 修改 `trigger`、`risk_level`、`required_permissions`、`inputs`、`steps`、`outputs` 或 `validator` 后，active procedure 必须降回 `draft`，清空旧 success / failure 计数，并标记 `review_status = edited_requires_review`。
- 每次编辑必须记录 reviewer、changed fields、from / to status、previous counts 和 note。

Review decisions:

| Decision | Effect |
|---|---|
| `approved` | `draft` 必须带 evidence refs 才能升级 `verified`；`edited_requires_review` / `decayed_requires_review` 必须带 fresh evidence refs 才能清除复审要求；其他 `verified` / `habit` 仅记录复审 |
| `needs_changes` | `verified` / `habit` 降回 `draft`，等待修正和重新验证 |
| `rejected` | procedure 进入 `disabled`，不再参与 retrieval |

Review record 必须写入 `reviews[]` 和 `audit_notes[]`，并更新 `review_status`、`last_reviewed_at` 和 `reviewed_by`。

## 检索与执行规则

任务开始时，在 full Planner 之前进行 procedure retrieval：

1. 根据 task goal、current goal、project state、user preference 检索候选 procedure。
2. 过滤 `disabled` / `deprecated`。
3. 检查 `trigger.required_context` 是否满足。
4. 检查权限和风险。
5. 默认 trigger similarity threshold 使用 `0.6`，避免不同重复任务只因共享通用词或 `reports/*` 路径而误命中；低于阈值必须进入 rejected candidates。
6. 输出 match decision artifact：
   - matched procedure id
   - confidence
   - reason
   - rejected candidates
   - required confirmation

执行规则：

- `draft` 只能建议，不能自动执行。
- `verified` 低风险可执行，但必须跑 validator。
- `habit` 低风险可 fast path；中高风险必须请求确认。
- 任何 procedure 都不能绕过 permission manager。
- 相似任务命中已有 `verified` / `habit` 且本次 validation passed 时，必须把新的 evidence refs、experience id、task id 和 success_count 回写到该 procedure，不能再生成重复 draft。
- validator 失败后必须增加 `failure_count` 并按规则降级或禁用。

## Promotion Rules

Draft -> Verified:

- 至少 1 条 passed validation evidence。
- source experience refs 存在。
- trigger、steps、validator 都完整。

Verified -> Habit:

- 至少 3 次 passed validation。
- 最近没有同类 failure。
- risk_level 不是 `high`，除非显式要求人工确认。
- 用户没有禁用该 procedure。

Habit -> Skill promotion candidate:

- habit 稳定。
- 输入输出契约清楚。
- 有独立测试或验证脚本。
- 有迁移或分享价值。

Decay / Downgrade:

- validator 失败可从 habit 降级到 verified。
- 连续失败可 disabled。
- 项目环境变化后必须重新验证。
- `learning decay --type procedure` 将 active procedure 降回 `draft`，清空旧 success / failure 计数，降低 confidence，并标记 `review_status = decayed_requires_review`。
- `learning decay --type habit` 将 habit 降回 `verified`，清空旧 success / failure 计数，降低 confidence，并标记 `review_status = decayed_requires_review`；降级后的对象不再作为 habit fast path candidate。
- `decayed_requires_review` 必须通过带 fresh evidence refs 的 `approved` review 才能清除，不能只靠无证据人工确认恢复自动复用资格。
- `disabled` / `deprecated` 对象不能直接 decay；必须先通过治理入口恢复或另行创建新候选。

## CLI / REPL 要求

首期至少保留管理入口设计：

```text
omniagent procedure list
omniagent procedure show <procedure_id>
omniagent procedure edit <procedure_id> --patch '{...}'
omniagent procedure review <procedure_id> --decision approved --evidence-ref <ref>
omniagent procedure disable <procedure_id>
omniagent procedure explain-match <task_or_task_id>
omniagent learning review
omniagent learning show --type experience|procedure|habit|skill <id>
omniagent learning history --type experience|procedure|habit|skill <id>
omniagent learning explain-match <task_or_task_id>
omniagent learning edit --type procedure|habit <id> --patch '{...}'
omniagent learning promote --type procedure|habit <id>
omniagent learning validate --type procedure|habit|skill <id> [--evidence-ref <ref>] [--version <version>]
omniagent learning decay --type procedure|habit <id> [--note <note>]
omniagent learning request-changes --type procedure|habit <id> [--note <note>]
omniagent learning approve --type procedure|habit|skill <id> [--evidence-ref <ref>]
omniagent learning reject --type procedure|habit|skill <id> [--note <note>]
omniagent learning disable --type procedure|habit|skill <id> [--note <note>]
omniagent learning enable --type procedure|habit|skill <id> [--note <note>]
omniagent learning rollback --type skill <skill_name> [--to-version <version>]
```

实际命令名可在实现时调整，但必须覆盖 list、show、edit、review、approve、reject、disable、enable、decay、request-changes、validate、rollback、explain-match、跨对象只读审计能力、对象级历史事件流、procedure / habit retrieval 匹配解释视图、procedure / habit 对象级编辑、升级、校验、衰减与要求修改、procedure / habit / skill 对象级审批 / 拒绝 / 禁用 / 恢复治理入口，以及 skill 对象级校验和回滚治理入口。

## 验收矩阵

| Requirement | Evidence needed | Status |
|---|---|---|
| 可从 passed experience 生成 draft | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_extracts_draft_from_passed_experience`；draft 引用 source experience | implemented |
| draft 提炼必须发生在验证之后 | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_extract_draft_requires_passed_validation_with_evidence`；`not_verified` / `partial` / `failed` 或缺少 evidence 的 experience 不会生成 draft | implemented |
| 无 evidence 的 draft 不能 verified | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_draft_without_evidence_cannot_be_verified`；promotion 被拒绝 | implemented |
| 相似任务可命中 procedure | `tests/test_procedure_memory.py::ProcedureMemoryRuntimeIntegrationTest::test_runtime_writes_procedure_match_artifact_before_planner`；match decision artifact 记录原因 | implemented |
| 不相关重复任务不能因通用词误命中 | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_unrelated_report_task_does_not_match_existing_procedure`；低于默认阈值的候选进入 rejected candidates | implemented |
| 命中已有 verified procedure 后会累计验证成功而不是生成重复 draft | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_validator_success_increments_verified_procedure_with_lineage` 和 `tests/test_procedure_memory.py::ProcedureMemoryRuntimeIntegrationTest::test_runtime_updates_matched_verified_procedure_instead_of_new_draft`；success_count、source experience / task lineage 和 validator evidence 回写到原 procedure | implemented |
| validator 失败会降级或禁用 | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_validator_failure_downgrades_verified_procedure` 和 `test_consecutive_validator_failures_disable_procedure`；failure_count 和 status 更新 | implemented |
| habit promotion 只能来自多次验证成功 | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_habit_promotion_requires_three_verified_successes` 和 `test_verified_procedure_promotes_to_low_risk_habit`；低于阈值被拒绝 | implemented |
| habit fast path 不绕过权限 | `tests/test_procedure_memory.py::ProcedureMemoryRuntimeIntegrationTest::test_runtime_writes_accepted_habit_fast_path_artifact`、`test_runtime_high_risk_habit_requires_confirmation_and_no_fast_path`；`tests/test_execution_policy.py::ExecutionPolicyTest::test_habit_candidate_does_not_bypass_static_risk_gate`；高风险 habit 仍需确认且 policy gate 不会接受 fast path | implemented |
| 用户可禁用 procedure | `tests/test_procedure_memory.py::ProcedureMemoryRuntimeIntegrationTest::test_cli_disable_prevents_future_matches`；disabled procedure 不再命中 | implemented |
| 用户可修正 procedure 且不绕过验证 | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_edit_behavior_fields_downgrades_habit_for_review`；行为字段编辑会降回 draft 并写 audit | implemented |
| 用户可审核 procedure 学习内容 | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_review_approved_draft_promotes_to_verified_with_evidence` 和 `test_review_rejected_disables_procedure`；approved / rejected 有状态转移和 review 记录 | implemented |
| CLI 暴露 edit / review 入口 | `tests/test_procedure_memory.py::ProcedureMemoryRuntimeIntegrationTest::test_cli_edit_and_review_procedure`；JSON 输出包含 `review_status` 和 `reviews` | implemented |
| CLI 暴露跨对象 learning review / show 入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_learning_review_unifies_procedure_and_skill_audit_items`、`test_learning_review_includes_experience_audit_items`、`test_cli_learning_review_outputs_json`、`test_cli_learning_review_filters_experience_items`、`test_cli_learning_review_filters_habit_items` 和 `test_cli_learning_show_outputs_experience_detail`；统一返回 experience / procedure / habit / skill 的 review status、risk、findings、lineage 和单对象 raw payload | implemented |
| CLI 暴露跨对象 learning history 入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_history_outputs_audit_events`；`learning history --type experience|procedure|habit|skill` 统一返回 experience recorded / validated、procedure audit / review 和 skill lifecycle / lineage 事件流 | implemented |
| CLI 暴露 procedure / habit retrieval 匹配解释入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_explain_match_outputs_habit_audit_context`；`learning explain-match <task_or_goal>` 复用 procedure match，并返回 matched audit item、rejected candidate items、confirmation requirement 和 suggestion mode | implemented |
| CLI 暴露 procedure / habit 对象级编辑治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_edit_controls_procedure_and_habit_objects`；`learning edit --type procedure|habit` 复用 procedure edit gate，返回 operation payload、previous_status 和 audit record，habit 行为字段编辑会降回 draft 并要求复审 | implemented |
| CLI 暴露 procedure / habit 对象级升级治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_promote_controls_procedure_and_habit_objects`；`learning promote --type procedure` 复用 habit promotion gate，`learning promote --type habit` 复用 skill promotion gate，返回 operation payload、target_type、target_id 和 validation / registration evidence | implemented |
| CLI 暴露 procedure / habit / skill 对象级校验治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_validate_controls_procedure_habit_and_skill_objects`；`learning validate --type procedure|habit` 复用 procedure review gate 和 evidence refs，`learning validate --type skill` 复用 skill lifecycle validation gate，返回 operation payload、previous_status、review / validation evidence、approval policy、可选 approval record 和单对象审计 item | implemented |
| CLI 暴露 procedure / habit 对象级衰减治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_decay_controls_procedure_and_habit_objects`；`learning decay --type procedure|habit` 撤回 stale procedure / habit 自动复用资格，清空旧 success / failure 计数，写入 `decayed` audit event，并标记 `decayed_requires_review`；后续 approval 必须带 fresh evidence refs | implemented |
| CLI 暴露 procedure / habit 对象级要求修改治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_request_changes_controls_procedure_and_habit_objects`；`learning request-changes --type procedure|habit` 复用 `needs_changes` review gate，verified procedure / habit 会降回 draft 并进入 review queue | implemented |
| CLI 暴露 procedure / habit / skill 对象级审批治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_approve_controls_procedure_and_skill_objects`；`learning approve --type procedure|habit|skill` 复用 procedure review gate 或 skill approval record，返回 operation payload、previous_status、review / approval record 和单对象审计 item | implemented |
| CLI 暴露 procedure / habit / skill 对象级拒绝治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_reject_controls_procedure_habit_and_skill_objects`；`learning reject --type procedure|habit|skill` 复用 procedure rejected review 或 skill rejected approval record，rejected former habit 仍可通过 `--include-disabled` 回到 habit 审计视图 | implemented |
| CLI 暴露 procedure / habit / skill 对象级禁用和恢复治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_disable_and_enable_controls_learning_objects`；`learning disable --type procedure|habit|skill` 返回 operation payload、保留 previous_status 或 registration note，disabled former habit 可通过 `--include-disabled` 回到 habit 审计视图；`learning enable --type procedure|habit|skill` 从 audit note 恢复 procedure / habit 状态，skill enable 复用 lifecycle activation gate | implemented |
| CLI 暴露 skill 对象级回滚治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_rollback_controls_skill_object`；`learning rollback --type skill` 复用 skill lifecycle rollback gate，返回 operation payload、previous_version、target_version、registration record 和回滚后的单对象审计 item | implemented |

## 实现证据

- Schema / storage / matching: `src/omniagent/procedure_memory.py`
- Runtime retrieval artifact: `workspace/tasks/<task_id>/procedure/procedure_match.json`
- Runtime habit artifact: `workspace/tasks/<task_id>/procedure/habit_fast_path.json`
- Policy artifact: `workspace/tasks/<task_id>/plan/policy_decision.json` includes `habit_fast_path.policy_gate`
- Planner payload: `procedure_match` 在 full Planner 前生成并注入 request payload
- CLI: `omniagent procedure list` / `show` / `edit` / `review` / `disable` / `explain-match` / `promote-habit`
- Learning governance: `omniagent learning review` / `omniagent learning show` / `omniagent learning history` / `omniagent learning explain-match` / `omniagent learning edit` / `omniagent learning promote` / `omniagent learning validate` / `omniagent learning decay` / `omniagent learning request-changes` / `omniagent learning approve` / `omniagent learning reject` / `omniagent learning disable` / `omniagent learning enable` / `omniagent learning rollback`
- Tests: `tests/test_procedure_memory.py`、`tests/test_learning_governance.py`

## 非目标

- 不在首期自动生成可分享 skill。
- 不让 draft procedure 自动执行。
- 不把 procedure memory 当成普通向量召回。
- 不用一次成功直接升级 habit。
