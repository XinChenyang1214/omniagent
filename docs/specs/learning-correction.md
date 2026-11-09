# Learning Correction Spec

Last reviewed: 2026-07-31

## 文档状态

- Document type: spec
- Workstream: `META-010` / Learning correction
- Document status: active
- Implementation status: verified
- Authority scope: procedure / habit 复用失败、过期或用户修正后的纠错、降级、冻结、解释和恢复证据契约
- Completion evidence: [`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](../workstreams/completed/AGENT_USABILITY_LEARNING_LOOP_MVP.md)

## 目标

Learning correction 证明 Agent 不只会沉淀和复用经验，也能治理错误经验。

当 procedure / habit 复用失败、过期或被用户修正后，系统必须：

1. 写入失败或修正来源的 experience record。
2. 将相关 procedure / habit 降级、冻结或禁用，防止继续自动复用。
3. 能解释为什么旧记忆不再复用。
4. 在用户复审或新 validation evidence 通过后，恢复到可复用状态。
5. 将纠错、降级、复审、恢复证据接入真实评测 evidence index / dashboard。

## 状态转换

```text
verified procedure --reuse failure--> draft + review_status=correction_required
habit --reuse failure--> verified + review_status=correction_required
procedure / habit --consecutive failures--> disabled
procedure / habit --user edit / request-changes / decay--> non-auto-reuse review state
review-required object --approved with fresh evidence--> verified / habit reusable state
disabled object --enable--> restored active status
```

## 运行时证据

复用失败任务必须写入：

```text
workspace/tasks/<task_id>/learning/correction.json
```

Required fields:

| Field | Meaning |
|---|---|
| `schema_version` | `learning-correction/v1` |
| `task_id` | 触发纠错的任务 |
| `procedure_id` | 被纠错的 procedure / habit |
| `object_type` | `procedure` or `habit` |
| `trigger` | `validator_failure`、`user_correction`、`decay`、`disable`、`enable`、`review_approved` 等 |
| `previous_status` | 纠错前状态 |
| `new_status` | 纠错后状态 |
| `previous_review_status` | 纠错前 review 状态 |
| `new_review_status` | 纠错后 review 状态 |
| `failure_count` | 纠错后失败计数 |
| `frozen` | 是否退出自动复用 |
| `evidence_refs` | failure / validation / review 证据引用 |
| `reason` | 人可读原因 |
| `next_actions` | 可执行的 review / edit / approve / enable / explain 命令提示 |

## 输出和解释规则

- `learning explain-match <task_or_goal>` 必须把 disabled / draft / review-required 的旧记忆放入 rejected candidates，并说明拒绝原因。
- 当失败导致 habit 降级到 verified，后续匹配不得再走 habit fast path。
- 当失败导致 procedure 降级到 draft 或 disabled，后续匹配不得自动复用。
- 用户通过 `learning approve` / `learning validate` 并提供 fresh evidence 后，可恢复 verified / habit 的可复用资格。
- 用户通过 `learning enable` 恢复 disabled 对象时，必须保留 previous / restored status 审计事件。

## 验收矩阵

| Requirement | Evidence needed | Status |
|---|---|---|
| 复用失败记录失败经验和 correction artifact | `src/omniagent/runtime.py` 在 matched procedure / habit 失败后写 `experience_record.json` 与 `learning/correction.json`；`tests/test_learning_feedback.py::LearningFeedbackIntegrationTest::test_failed_reuse_feedback_records_correction_and_blocks_future_match` 覆盖 `trigger=validator_failure`、status transition、evidence refs 和 next actions | verified |
| 失败后退出自动复用 | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_validator_failure_downgrades_verified_procedure` 覆盖 `review_status=correction_required` 后 explain-match rejected；连续失败禁用和 habit 降级仍由 procedure memory 回归覆盖 | verified |
| 用户修正或 decay 后进入复审队列 | `learning edit` / `learning decay` / `learning request-changes` 产生 review-required status，review / history 能看到对象审计；纠错复审状态进入 learning governance findings | implemented |
| 新证据复审后恢复可复用 | `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_correction_review_with_fresh_evidence_restores_reuse` 和 `tests/test_learning_feedback.py::LearningFeedbackIntegrationTest::test_failed_reuse_feedback_records_correction_and_blocks_future_match` 覆盖 approved fresh evidence 后恢复 verified reuse | verified |
| evidence index / dashboard 可消费纠错闭环 | `scripts/run_real_world_evals.py` 读取 `learning_corrections`，支持 query / dashboard 的 Learning Correction Cases 视图；`tests/test_real_world_eval_diagnostics.py` 覆盖 assertion、index、query 和 dashboard | verified |
| 真实 learning-loop 评测覆盖完整闭环 | `learning_loop_correction_recovery` 验证失败、降级、解释、复审、恢复和 dashboard evidence；`eval_runs/20260616_020213/` 为 1 passed / 0 failed / 0 skipped | verified |

## 非目标

- 不让失败学习对象绕过 review / validation gate 自动恢复。
- 不把单次失败直接删除历史记忆；只做可审计降级、冻结或禁用。
- 不扩大到多用户同步、远端记忆或图形化 UI。
