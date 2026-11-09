# Experience Record Spec

Last reviewed: 2026-07-31

## 文档状态

- Document type: spec
- Workstream: `META-010`
- Document status: active
- Implementation status: implemented
- Authority scope: 任务经验记录的最小数据契约、写入时机和学习升级边界
- Completion evidence: [`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](../workstreams/completed/AGENT_USABILITY_LEARNING_LOOP_MVP.md)

## 目标

`ExperienceRecord` 让每次任务都留下可回放、可验证、可学习的结构化经验。它不是日志摘要，也不是自然语言复盘，而是 Learning Engine 后续提炼 procedure / habit / skill 的事实输入。

核心原则：

```text
No evidence, no promotion.
```

没有 validation evidence 的任务经验只能作为候选经验，不能升级为 verified procedure、habit 或 skill。

## 写入时机

每个任务进入终态时必须写入一条 experience record：

- `succeeded`
- `failed`
- `cancelled`
- `blocked`
- `permission_denied`
- `partially_completed`

写入顺序：

1. Execution Runtime 完成或失败。
2. Validator / Critic / Completion evaluator 写入 evidence。
3. Experience writer 汇总 task 输入、动作、产物、验证结果和失败归因。
4. Learning Engine 只读取已完成写入的 experience record。

## 存储规则

Canonical path:

```text
workspace/tasks/<task_id>/experience/experience_record.json
```

Optional index:

```text
workspace/experience/index.jsonl
```

Rules:

- canonical record 必须跟 task artifact 放在一起，便于回放。
- index 只保存可检索摘要，不保存敏感原文。
- record 必须引用 artifact path，不复制大文件内容。
- record 必须标明 schema version。

## Schema

Required fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | schema version |
| `experience_id` | string | stable id |
| `task_id` | string | source task id |
| `thread_id` | string/null | source thread id |
| `goal_id` | string/null | current goal id |
| `created_at` | string | ISO timestamp |
| `status` | string | terminal status |
| `user_goal` | string | user-facing goal |
| `definition_of_done` | list[string] | task-specific done criteria |
| `context_refs` | list[string] | docs/state/memory references used |
| `actions` | list[object] | executed steps |
| `artifacts` | list[object] | produced or consumed artifacts |
| `validation` | object | evidence and verdict |
| `failure` | object/null | failure layer and cause |
| `learning_candidates` | list[object] | possible lessons/procedure candidates |
| `sensitive_data_policy` | object | redaction/omission summary |

Action item:

| Field | Type | Meaning |
|---|---|---|
| `node_id` | string/null | graph node id if applicable |
| `tool_or_skill` | string | executor, tool, skill, or procedure used |
| `input_summary` | string | safe summary of input |
| `output_summary` | string | safe summary of output |
| `status` | string | step status |
| `artifact_refs` | list[string] | produced artifacts |

Validation object:

| Field | Type | Meaning |
|---|---|---|
| `verdict` | string | `passed` / `failed` / `partial` / `not_verified` |
| `method` | string | `test` / `artifact_check` / `critic` / `completion` / `user_acceptance` |
| `evidence_refs` | list[string] | files or artifacts proving verdict |
| `notes` | string | concise explanation |

Learning candidate:

| Field | Type | Meaning |
|---|---|---|
| `type` | string | `preference` / `project_fact` / `procedure_candidate` / `failure_pattern` |
| `statement` | string | candidate lesson |
| `confidence` | number | 0.0-1.0 |
| `requires_review` | boolean | whether human confirmation is required |
| `evidence_refs` | list[string] | supporting evidence |

## Learning Rules

- `validation.verdict = passed` is required for automatic procedure verification.
- `not_verified` records may still be searchable but cannot promote.
- `failed` records are valuable as negative signal and failure pattern evidence.
- User correction after task completion must append a new experience or update validation with an audit trail.
- Learning Engine may propose candidates, but promotion must check evidence refs exist.

## Redaction Rules

The writer must not store:

- API keys, cookies, tokens, passwords, private keys.
- Raw `.env` values.
- Full sensitive request/response bodies.

It may store:

- key names without values.
- file paths.
- short safe summaries.
- hashes/fingerprints when useful.

## 验收矩阵

| Requirement | Evidence needed | Status |
|---|---|---|
| 每个 terminal task 写入 record | `tests/test_experience_record.py` 覆盖 success / failure / permission denied；runtime planning failure 和 non-plan decision 也接入 writer | implemented |
| record 引用 artifacts 而不是复制大内容 | `tests/test_experience_record.py::ExperienceRecordIntegrationTest::test_success_task_writes_record_and_index_without_raw_payload_copy` | implemented |
| validation evidence 控制 promotion | `src/omniagent/experience.py` 仅在 `validation.verdict = passed` 时生成 `procedure_candidate`，且仍标记 `requires_review=true`；`tests/test_experience_record.py::ExperienceRecordIntegrationTest::test_success_without_validation_does_not_create_procedure_candidate` 验证成功但未验证的 record 不生成 procedure candidate，也不能提炼 procedure draft | implemented |
| failure record 可作为 negative signal | `tests/test_experience_record.py::ExperienceRecordIntegrationTest::test_failed_task_writes_failure_record`；失败记录生成 `failure_pattern` candidate | implemented |
| 敏感值不落盘 | `tests/test_experience_record.py::ExperienceRecordIntegrationTest::test_success_task_writes_record_and_index_without_raw_payload_copy`；敏感 key/value 只进入 redacted summary | implemented |

## 实现证据

- Schema / writer: `src/omniagent/experience.py`
- Runtime integration: `src/omniagent/runtime.py` 在 graph execution、planning failure、non-plan decision 结束路径写入 record
- Storage: `workspace/tasks/<task_id>/experience/experience_record.json`
- Index: `workspace/experience/index.jsonl`
- Tests: `tests/test_experience_record.py`

## 非目标

- 不替代完整 task logs。
- 不作为用户最终报告。
- 不直接生成 skill。
- 不把所有 LLM prompt/response 原文复制进 experience record。
