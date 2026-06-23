# Issue Tracker

记录当前未关闭问题与近期已关闭问题。

> **修复规则**：碰到新 bug 时，**先问"这反映了哪个 META"**。如果是新的 META，先立 META 项再考虑补丁；如果命中已有 META，补丁直接进 git log，commit message 明确写 "根治待 META-XXX"。

---

## 当前优先级状态（2026-06-15）

**当前主线：`META-010` Agent usability and learning loop MVP。**

任务文档：[`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](./docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md)

当前规则：

- `REAL-EVAL-006` 复杂真实场景矩阵已归档为旧实现回归集
- 当前按 [`docs/product-architecture.md`](./docs/product-architecture.md) 重新开发为 learning-loop-centered personal Agent OS
- 当前正在推进 `META-010` Agent usability and learning loop MVP
- Phase 1 Agent state、Phase 2 Experience records、Phase 3 Procedure memory、Phase 4 Habit fast path、Phase 5 Skill promotion、Phase 6 Learning feedback 和 Phase 7 Learning correction 已实现；learning-loop 真实评测、learning feedback task-ending reply、learning correction artifact / frozen reuse / fresh evidence restore、procedure edit / review、统一 learning review / show / history / explain-match / edit / promote / validate / decay / request-changes / approve / reject / disable / enable / rollback 入口、experience review、habit review、skill validation / approval / rejection / disable / enable / rollback、跨 eval evidence index、evidence index scenario / category / requires / skip-reason / permission / completion-layer / policy-decision / skipped-stage / policy-risk-flag / policy-reason / policy-context-budget / request-payload-stats / node-status / critic-action / memory-retrieval / llm-usage / promotion-generation-type / promotion-source-target / learning-kind / learning-validation / skill-validation / review-decision / audit-event 查询视图、evidence dashboard regression commands（含 scenario / procedure-id / habit-id / lineage-skill / promotion-generation-type / permission-summary / learning-kind / learning-validation / skill-validation / review-decision / audit-event 动态查询）/ eval coverage / skipped eval / model capability / node execution / critic action / completion evaluation / memory retrieval / LLM usage / runtime cost hotspot / execution policy / Planner skipped / policy risk flag / policy reason / policy context budget / request payload stats / permission governance / habit fast path / learning match explanation / learning gate / learning feedback / learning correction / learning governance / learning history / learning approval / learning promotion / learning validation / learning edit / learning request-changes / learning decay / learning disable / learning restore / learning rollback / learning rejection / learning audit gap / skill lifecycle governance / skill promotion coverage / skill approval gate 视图、JSON / JSONL data habit promotion、active generated habit direct execute、CSV / TSV data habit promotion、SQLite data habit promotion、config / YAML / XML / dotenv / INI / properties habit promotion、plain text habit promotion、markdown habit promotion、HTML habit promotion、archive / tar.gz habit promotion、datetime habit promotion、read-only Git habit promotion（手动审批）、Python habit promotion（手动审批）、HTTP habit promotion（手动审批）、browser fetch habit promotion（手动审批）和 safe shell habit promotion（手动审批）已补齐，下一步按真实稳定 habit 继续扩展 skill promotion 类型，继续扩展学习治理入口，并继续把 evidence dashboard 接入更多回归消费入口
- 2026-06-15 Phase 6 Learning feedback 已收口验证：任务结束回复、`learning/feedback.json`、CLI / chat 可见反馈、连续任务真实评测和 `learning_feedbacks` evidence dashboard 入口已补齐
- 2026-06-16 Phase 7 Learning correction 已收口验证：matched procedure / habit 失败后写 `learning/correction.json`，旧学习对象进入 `correction_required`、降级或冻结，explain-match 解释不再复用，fresh evidence approval 后恢复，`learning_corrections` evidence dashboard 入口已补齐
- `META-009` 首轮成果作为 Execution Runtime / habit fast path 迁移输入，不再作为独立当前主线
- 已有 P2 项保留记录，等待重新排期
- `ISSUE-015` Browser 中文 URL 编码问题已在 `BASECAP-001` 阶段 0 完成
- `ISSUE-007` skill_registry_entries 全量发送问题已在 `BASECAP-001` 阶段 4 完成

### ✅ 已完成首要问题

| ID | 一句话 | 优先级 | 状态 | 备注 |
|---|---|---|---|---|
| **CAP-EVAL-001** | 复杂真实评测前先补齐任务完成度评估、模型能力归因和真实 embedding | P0 | ✅ 完成 | 默认非 Git 基线 `eval_runs/20260521_073254/`：27 passed / 0 failed / 11 skipped；复杂矩阵 `eval_runs/20260521_092801/`：8 passed / 0 failed / 0 skipped |
| **BASECAP-001** | 建设标准基础能力库，补齐 JSON / CSV / datetime / HTTP / safe shell 等 builtin skill | P0 | ✅ 完成 | 阶段 0-4 已完成 |
| **BASECAP-002** | 标准 skill 库 v1 首批，补齐 SQLite / Markdown / Git / HTML / Archive / Config | 用户指定 | ✅ 完成 | 首批 6 个 builtin skill 已落地，新增真实评测场景定义 |
| **REAL-EVAL-005** | 第二批真实场景矩阵，覆盖错误恢复、复杂多轮、规划合理性和产物质量 | P0 | ✅ 完成 | 最终 `eval_runs/20260512_032854/` 为 10 passed / 0 failed / 0 skipped |

---

## 📊 全局索引（2026-06-15 更新）

> **当前状态**：BASECAP-001、REAL-EVAL-005、CAP-EVAL-001 和 META-009 首轮均作为旧实现基础归档。当前新主线是 META-010：Phase 1 Agent state、Phase 2 Experience records、Phase 3 Procedure memory、Phase 4 Habit fast path、Phase 5 Skill promotion、Phase 6 Learning feedback 和 Phase 7 Learning correction 已实现并有真实评测证据。真实评测场景文件总数 43；`learning_loop_feedback_sequence` 最新通过基线为 `eval_runs/20260615_095855/`，`learning_loop_correction_recovery` 最新通过基线为 `eval_runs/20260616_020213/`，并已接入 `learning_feedbacks` / `learning_corrections` evidence kind 和 Learning Feedback / Learning Correction Cases dashboard 视图。下一步按真实稳定 habit 继续扩展 skill promotion 类型，并继续深化学习治理回归。
> 历史修复记录见 git log。

### 🔴 META（设计/架构层）

| ID | 一句话 | 优先级 | 备注 |
|---|---|---|---|
| **META-010** | 按最新产品架构重开发为以学习循环为中心的个人 Agent OS | 🔴 当前主线 | 见 [`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](./docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md) |
| **META-002** | 路由本体把管线指令/查询/生命周期/闲聊塞同一个 enum，靠关键词列表分流 | P2 暂停 | `CAP-EVAL-001` 完成后重新排序 |
| **META-005** | 失败不会反向传播到 router/dispatcher/planner，同一个坑会反复掉 | P2 暂停 | `CAP-EVAL-001` 完成后重新排序 |
| **META-009** | Runtime 缺少按任务确定性分层的执行策略，低风险结构化任务和 skill manifest 展开仍未完全按需 | 📦 已归档 | 第一轮已落地 ExecutionPolicy 与 Planner manifest 数据分层；旧专项见 [`RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md`](./docs/archive/pre-product-redesign/RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md) |

### 🟡 ISSUE（待排期单点功能/优化项）

| ID | 一句话 | 优先级 | 备注 |
|---|---|---|---|
| ISSUE-003 | 分层召回（按置信度路由结果） | P2 暂停 | `CAP-EVAL-001` 完成后重新排序 |
| ISSUE-004 | Token 超限无裁剪策略 | P2 暂停 | `CAP-EVAL-001` 完成后重新排序 |

### 🟠 REAL-EVAL（当前真实评测工作）

`REAL-EVAL-006` 已归档为复杂回归集。该批场景必须继续使用 `scripts/run_real_world_evals.py` 的隔离 workspace、`diagnostics.md`、completion evaluation、embedding summary 和 failure attribution 归因闭环。

2026-05-20 追加 5 个复杂专项场景：上下文预算与相关召回、多轮状态查询后的指代、配置缺 key 的 tool 层失败归因、archive 路径穿越防护、completion evaluation 覆盖错误成功结果。

2026-05-21 首轮复杂专项矩阵已跑通：初跑 `eval_runs/20260520_160630/` 为 2 passed / 3 failed；修复路由状态查询误判、显式 file payload 被 Planner 改写、completion evaluator 证据不足、config report Planner 超时后，最终 `eval_runs/20260520_165848/` 为 5 passed / 0 failed / 0 skipped。

2026-05-21 最终归档：默认非 Git 基线 `eval_runs/20260521_073254/` 为 27 passed / 0 failed / 11 skipped；复杂矩阵 `eval_runs/20260521_092801/` 为 8 passed / 0 failed / 0 skipped；本轮新增确定性文件/JSON/config/browser payload 计划、task-level Critic LLM 失败降级和多轮指代复合文件编辑回归。

2026-05-21 `META-009` 首轮记录：新增 `explicit_file_payload_fast_path` 场景，当前场景定义总数 39；`eval_runs/policy_explicit_check/20260521_104629/` 验证低风险显式 file payload 走 `direct_execute` 且无 `planner_v1` 调用；`eval_runs/20260521_104512/` 和 `eval_runs/policy_complex_context_check/20260521_104738/` 验证当 expected output 明确要求 Planner context 时回退 `plan_required` 并保留原 context 断言。一次默认基线 `eval_runs/20260521_100101/` 因旧断言与 direct execute 验收对象冲突中止，不作为通过基线。

2026-06-12 `META-010` learning-loop 记录：新增 `learning_loop_habit_promoted_skill_lineage` 场景，验证预置 habit-promoted skill 可被真实 runtime 使用，且 workspace 保留 `procedures/habits/<id>.json` 和 `skills/managed/<skill>/lineage.json`。单场景验证 `eval_runs/20260612_034828/` 为 1 passed / 0 failed / 0 skipped。

2026-06-13 `META-010` learning-loop 记录：新增 `learning_loop_jsonl_habit_promoted_skill_lineage` 场景，验证 JSONL habit-promoted skill 可写出 `reports/jsonl_event_names.md`，lineage 记录 `generation_type=json` 和 `source_targets=[jsonl]`，且 active generated habit skill 在 contract / permission gate 下走 `ExecutionPolicy.direct_execute` 并跳过 Planner。单场景验证 `eval_runs/20260613_042808/` 为 1 passed / 0 failed / 0 skipped；`poetry run python scripts/run_real_world_evals.py --check-only` 校验 41 个场景通过。

2026-06-15 `META-010` learning feedback 记录：新增 `learning_loop_feedback_sequence` 连续任务场景，验证 learning feedback 在 draft 生成、procedure 审批、verified reuse、habit fast path、explain-match、edit 和 disable 治理链路中的证据。单场景验证 `eval_runs/20260615_095855/` 为 1 passed / 0 failed / 0 skipped；`poetry run python scripts/run_real_world_evals.py --check-only` 校验 42 个场景通过；`--query-evidence-index --evidence-scenario learning_loop_feedback_sequence --evidence-learning-kind learning_feedbacks` 可查到 `learning_feedbacks`，`--evidence-dashboard` 已生成 Learning Feedback Cases 视图。

2026-06-16 `META-010` learning correction 记录：新增 `learning_loop_correction_recovery` 连续任务场景，验证 verified procedure 复用失败后写 `learning/correction.json`、降级到 `correction_required`、explain-match 不再自动复用旧对象，并在 fresh evidence approval 后恢复 verified reuse。单场景验证 `eval_runs/20260616_020213/` 为 1 passed / 0 failed / 0 skipped；`poetry run python scripts/run_real_world_evals.py --check-only` 校验 43 个场景通过；`--query-evidence-index --evidence-learning-kind learning_corrections` 可查到纠错证据，`--evidence-dashboard` 已生成 Learning Correction Cases 视图。

2026-06-12 `META-010` learning governance 记录：新增 `omniagent procedure edit` / `review`，用户可受控修正 procedure 的 trigger / validator / contract 等字段；行为字段编辑会撤回 habit / verified 自动复用资格并要求复审。回归测试见 `tests/test_procedure_memory.py`。

2026-06-12 `META-010` evidence index 记录：`scripts/run_real_world_evals.py --build-evidence-index` 可从已有 `eval_runs/` 汇总跨 run evidence，生成 `eval_runs/evidence_index.json` / `.md`。当前本地索引覆盖 67 个 run、463 个 case，并记录 failure layer、skill usage、permission summary、completion / embedding summary、procedure / habit artifacts 和 skill lineage。

2026-06-23 `META-010` general agent usability audit 记录：新增 `scripts/run_real_world_evals.py --audit-general-agent-usability`，把 `general_agent_usability` 的 5 类任务、learning sequence / tool-choice / permission boundary 定义覆盖、required scenario passed evidence 和真实 LLM tool-choice baseline 汇总成目标级审计。离线 `OMNIAGENT_LLM_PROVIDER=test` 最新基线 `eval_runs/general_agent_usability_offline_latest/20260623_022106/` 为 15 passed / 0 failed / 0 skipped；重建后的本地 evidence index 覆盖 124 个 run、558 个 case；当前审计已确认定义覆盖和 required passed evidence，剩余缺口为 `real_llm_tool_choice_baseline`。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_general_agent_usability_audit_reports_real_llm_gap`、`test_general_agent_usability_audit_accepts_complete_real_llm_evidence` 和 `test_general_agent_usability_audit_cli_allows_incomplete_report`。

2026-06-12 `META-010` evidence index 查询记录：`scripts/run_real_world_evals.py --query-evidence-index` 可读取已有 index，按 case status、failure layer、used skill、procedure id、habit id、skill lineage 和 scenario 过滤，并输出 Markdown 或 JSON 回归报告视图。回归测试见 `tests/test_real_world_eval_diagnostics.py`。

2026-06-13 `META-010` evidence index 查询扩展记录：`--query-evidence-index` 新增 `--evidence-category`、`--evidence-requires`、`--evidence-skip-reason`、`--evidence-permission`、`--evidence-completion-layer`、`--evidence-memory-retrieval`、`--evidence-llm-usage`、`--evidence-learning-kind`、`--evidence-learning-validation`、`--evidence-skill-validation`、`--evidence-learning-review-decision` 与 `--evidence-learning-audit-event`，可直接筛选 scenario category、requires tag、skip reason、permission summary entry、completion evaluation failure layer（含 `recorded` 语义过滤）、memory retrieval / embedding signal（`recorded`、`vector_match`、`vector_memory_entry`、`fallback`、`error`、`incompatible`、provider / model）、LLM usage / token / duration signal（`llm_called`、`no_llm`、`llm_unknown`、token bucket、duration bucket）、learning evidence kind（如 `habit_fast_paths`）、跨 procedure / habit approved review 与 skill lifecycle validation 的 learning validation evidence、skill lifecycle validation status（`passed` / `failed` / `recorded`）、procedure / habit review decision 和 audit event，查询摘要和 Markdown 输出也会显示 category / requires / skip-reason / permission / completion-layer / memory-retrieval / llm-usage / learning-kind / learning-validation / skill-validation / review-decision / audit-event 分布。后续扩展新增 `--evidence-promotion-generation-type` 和 `--evidence-promotion-source-target`，可按 skill lineage 中的 generation type（如 `json`）和 source target（如 `tsv` 或 `builtin.file.basic`）过滤，并输出 `by_promotion_generation_type` / `by_promotion_source_target` 分布。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage`。

2026-06-13 `META-010` evidence index / dashboard 扩展记录：`--query-evidence-index` 新增 `--evidence-policy-decision`，可按 `direct_execute` / `plan_required` 等 ExecutionPolicy decision 过滤，并在 query / dashboard summary 中输出 `by_policy_decision`；dashboard 新增 execution policy 固定回归消费入口和 policy decision 动态查询命令。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence index / dashboard 扩展记录：`--query-evidence-index` 新增 `--evidence-skipped-stage`，可按 `planner` 等 ExecutionPolicy skipped stage 过滤，并在 query / dashboard summary 中输出 `by_skipped_stage`；dashboard 新增 Planner skipped 固定回归消费入口和 skipped stage 动态查询命令，用于证明 fast path 实际跳过的阶段。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence index / dashboard 扩展记录：`--query-evidence-index` 新增 `--evidence-policy-risk-flag`，可按 `planner_context_required`、`network`、`ambiguous_reference` 等启用的 ExecutionPolicy risk / routing flag 过滤，并在 query / dashboard summary 中输出 `by_policy_risk_flag`；dashboard 新增 Policy Risk Flag 固定回归消费入口和 policy risk flag 动态查询命令，用于解释 fast path 为什么触发或为什么回退 Planner。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence index / dashboard 扩展记录：`--query-evidence-index` 新增 `--evidence-policy-reason`，可按 ExecutionPolicy reason 文本过滤（支持 glob，如 `*风险标记*`），并在 query / dashboard summary 中输出 `by_policy_reason`；dashboard 新增 Policy Reason 固定回归消费入口，用于直接聚合 direct_execute / plan_required 的触发或回退解释。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence index / dashboard 扩展记录：`--query-evidence-index` 新增 `--evidence-policy-context-budget`，可按 ExecutionPolicy `context_budget` 生成的 bucket 信号过滤（如 `skill_registry_entries:json_chars:50000+`），并在 query / dashboard summary 中输出 `by_policy_context_budget`；dashboard 新增 Policy Context Budget 固定回归消费入口，用于定位 skill registry、Planner registry、agent state、procedure match 和 habit fast path bootstrap 字段大小。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence index / dashboard 扩展记录：`--query-evidence-index` 新增 `--evidence-request-payload-stats`，可按 LLM `request_payload_stats` 生成的 bucket 信号过滤（如 `skill_registry_entries:json_chars:10000-49999`），并在 query / dashboard summary 中输出 `by_request_payload_stats`；dashboard 新增 Request Payload Stats 固定回归消费入口，用于定位 Planner / Critic / Completion request payload 字段大小和上下文膨胀热点。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence index / dashboard 扩展记录：`--query-evidence-index` 新增 `--evidence-node-status`，可按 TaskGraph `node_statuses` 生成的稳定信号过滤（如 `status:failed`、`skill:builtin.file.basic`、`action:write`、`error:present`），并在 query / dashboard summary 中输出 `by_node_status`；dashboard 新增 Node Execution 固定回归消费入口，用于定位执行节点状态、skill/action、attempts 和 error evidence。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard regression commands 现在会按 node status 的低基数排错信号生成动态查询命令（`status:*`、`attempts:*`、`error:*`），但不展开高基数的 `skill:*`、`executor:*` 或 `action:*` 信号，避免命令清单噪声过高。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence index / dashboard 扩展记录：`--query-evidence-index` 新增 `--evidence-critic-action`，可按 Critic `critic_actions` 生成的稳定信号过滤（如 `action:accept`、`summary:present`、`target:<id>`），并在 query / dashboard summary 中输出 `by_critic_action`；dashboard 新增 Critic Action 固定回归消费入口，并在 regression commands 中按 `action:*` 生成动态查询命令但不展开高基数 `target:*` 信号，用于定位 Critic verdict action、target 和 summary evidence。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard 新增 completion evaluation 固定回归消费入口，集中查看 `completion_failure_layers` 和 completion evaluator 产出的主失败归因，并通过 `--evidence-completion-layer recorded` 生成可复用查询命令。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard regression commands 现在会按 completion evaluation 的非 `none` failure layer 生成动态查询命令（如 `--evidence-completion-layer tool_or_skill`），但不为 `none` 生成排错入口，避免把成功/无归因 case 混入失败修复清单。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard 新增 memory retrieval 固定回归消费入口，集中查看 embedding summary、vector match / memory entry、fallback、error 和 incompatible match 证据，并通过 `--evidence-memory-retrieval recorded` 生成可复用查询命令。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard regression commands 现在会按 memory retrieval 的低基数状态信号生成动态查询命令（`failure_layer`、`vector_match`、`vector_memory_entry`、`fallback`、`error`、`incompatible`），但不展开 `provider:*` 或 `model:*`，避免把环境/模型命名造成的高基数差异混入回归命令清单。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard 新增 LLM usage 固定回归消费入口，集中查看真实触发 LLM 的 case、token bucket 和 duration bucket，并通过 `--evidence-llm-usage llm_called` 生成可复用查询命令。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard 新增 runtime cost hotspot 固定回归消费入口，集中查看 `tokens:10000+` 或 `duration:60s+` 的高成本 case，并通过 `--evidence-llm-usage tokens:10000+,duration:60s+` 生成可复用查询命令。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard regression commands 现在会按 LLM usage 的低基数 token / duration / call-state 信号生成动态查询命令（`llm_called`、`no_llm`、`llm_unknown`、`tokens:*`、`duration:*`）；固定视图命令会去重，避免重复输出 `--evidence-llm-usage llm_called`。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard summary 现在输出 `by_learning_review_decision` 和 `by_learning_audit_event`，regression commands 会按 procedure / habit review decision 与 audit event 生成动态查询命令，并复用固定 learning edit / request-changes 视图命令去重，方便直接定位 `approved` / `rejected` / `disabled` / `enabled` / `reviewed` 等学习治理对象事件。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard summary 现在输出 `by_learning_validation` 和 `by_skill_validation`，regression commands 会按 learning validation / skill validation 的低基数状态生成动态查询命令（如 `approved`、`recorded`、`passed`、`skill`），让 procedure / habit approved review 和 skill lifecycle validation 证据可从 runbook 直接进入回归消费入口。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard regression commands 现在会按 `by_learning_kind` 的单项 learning evidence kind 生成动态查询命令，并复用固定 learning view query command 去重，使 `procedure_memory_records`、`skill_lineages` 等对象级证据可直接从 runbook 单独进入回归消费入口。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard 新增 eval coverage 固定回归消费入口，集中查看 case status、category、requires 和 skip reason；evidence index 与 Markdown 报告同步保留 requires / skip_reason 字段，便于判断新增学习闭环能力是否进入真实评测矩阵而不是只停留在单测。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard 新增 skipped eval 固定回归消费入口，通过 `--evidence-status skipped` 聚合被 network / L4 / complex requires gate 跳过的 case，并配合 `--evidence-requires` / `--evidence-skip-reason` 精确复核跳过口径。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 记录：`scripts/run_real_world_evals.py --evidence-dashboard` 可从已有 index 生成 `eval_runs/evidence_dashboard.md`，固定包含失败修复入口、failure layer 查询命令、skill/tool failure、generated habit skill usage 和 learning lineage cases；也支持 JSON payload。回归测试见 `tests/test_real_world_eval_diagnostics.py`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 permission governance 和 habit fast path 两个固定回归消费入口，分别用于集中检查权限治理 case 与 habit reuse / validator evidence case。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning gate 固定回归消费入口，按 `experience_records` learning evidence kind 集中查看带 experience record 的 case，用于检查学习入口是否绑定 validation evidence。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 model capability 固定回归消费入口，集中查看 `failure_layer=model_capability`、completion failure layer 或 `model_capability_suspected` case，避免把模型能力边界误修成 Agent 流程补丁。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 skill approval gate 固定回归消费入口，集中查看 `generated.habit.*` 中触发 `system.exec`、`network`、`code_execution`、`dependency.install` 或 `filesystem.delete` 等审批敏感权限的 case，用于检查高风险 promotion 没有绕过人工审批和权限治理。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning match explanation 固定回归消费入口，集中查看 `procedure_matches`，并在 compact case 中输出 matched / suggestion mode / confirmation requirement / rejected candidate count，用于检查 `learning explain-match` 对应的检索解释证据可直接进入回归看板。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：evidence index 新增 `procedure_memory_records` learning evidence kind，记录 procedure / habit lifecycle、review 和 audit note；dashboard 新增 learning governance 固定回归消费入口，用于集中检查用户治理操作是否进入回归证据。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：evidence index 新增 `skill_lifecycle_records` learning evidence kind，从每个 eval case 的 SQLite lifecycle 表读取 skill registry、validation、approval 和 registration 摘要；learning governance dashboard 现在同时消费 procedure / habit audit evidence 和 skill lifecycle deprecate evidence，用于验证 `learning disable --type skill` 等治理操作进入回归视图。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_summarizes_runs_and_learning_artifacts` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：evidence index 的 `procedure_memory_records` 新增 `audit_events` / `latest_audit_event` / `restored_status`，dashboard 新增 learning restore 固定回归消费入口，集中查看 `learning enable` 的 procedure / habit 恢复和 skill deprecate -> activate 证据。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增独立 skill lifecycle governance 固定回归消费入口，直接按 `skill_lifecycle_records` 查看 skill registry、validation、approval 和 registration lifecycle evidence。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning approval 固定回归消费入口，集中查看 `learning approve` 的 procedure / habit approved review evidence 和 skill approved approval evidence，避免用户审批记录只埋在宽泛 governance 视图中。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning edit 固定回归消费入口，集中查看 procedure edited evidence，使用户修正过且需要复审的学习对象可从回归看板直接定位。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning request-changes 固定回归消费入口，集中查看 `learning request-changes` / `needs_changes` review evidence，确认要求修改的 procedure / habit 已退出自动复用并进入复审队列。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_learning_request_changes_view`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning decay 固定回归消费入口，集中查看 procedure / habit decayed audit evidence 和 `decayed_requires_review` review status，确认过期学习对象不会继续作为自动复用能力。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_learning_decay_view`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning disable 固定回归消费入口，集中查看 `learning disable` 的 procedure / habit disabled audit evidence 和 skill deprecated lifecycle evidence，并避免把 rejected former habit 误归入 disable 视图。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：evidence index 的 `procedure_memory_records` 新增 `review_decisions` / `latest_review_decision` 并能识别 rejected former habit；dashboard 新增 learning rejection 固定回归消费入口，集中查看 `learning reject` 的 procedure / habit rejected review evidence 和 skill rejected approval evidence。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning audit gap 固定回归消费入口，集中查看已有 procedure / habit / skill lineage 但缺少 procedure memory record、review / audit note、skill lifecycle validation / approval / registration evidence 的 case，优先暴露学习证据链断点。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning promotion 固定回归消费入口，集中查看 procedure -> habit 的 `promoted_to_habit` audit evidence、skill lineage 以及 habit-promotion skill lifecycle evidence，避免学习升级证据只埋在宽泛 governance / lineage 视图中。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：learning validation 固定回归消费入口已从 skill-only 扩展为同时集中查看 procedure / habit `procedure_memory_records` approved review evidence 和 `skill_lifecycle_records.validation_passed` / `last_validated_at` evidence，并改用 `--evidence-learning-validation "*"` 作为精确回归查询命令，确认 `learning validate --type procedure|habit|skill` 对应的校验证据能被回归看板直接定位，而不是只埋在宽泛 governance / lifecycle 视图中。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning rollback 固定回归消费入口，集中查看 `skill_lifecycle_records` 中的 rollback registration action，确认学习能力版本回退进入回归视图。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：dashboard 新增 learning history 固定回归消费入口，集中查看可被 `omniagent learning history` 展开的 `experience_records`、`procedure_memory_records` audit / review evidence 和 `skill_lifecycle_records` lifecycle events。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-12 `META-010` evidence dashboard 扩展记录：evidence index 的 `skill_lineages` 新增 `generation_type`，dashboard 新增 skill promotion coverage 固定回归消费入口，并在摘要中输出 `by_promotion_generation_type`，用于检查新增 habit-promoted skill 类型是否进入真实回归证据。后续扩展已将 `source_targets` 纳入 skill lineage、evidence index、summary 和 Markdown runbook，并输出 `by_promotion_source_target`，用于检查 `tsv` 等 alias 是否进入真实回归证据。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_summarizes_runs_and_learning_artifacts` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard payload 和 Markdown runbook 新增 `regression_commands`，自动列出 scenario check、evidence index rebuild、dashboard render、failure repair query、去重后的固定视图 query，以及按 `by_promotion_generation_type` / `by_promotion_source_target` 生成的 promotion generation type 和 source target 动态查询，方便从看板直接进入下一轮回归消费。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：`--evidence-dashboard` 新增 `--evidence-dashboard-commands-output`，可把 dashboard 的 `regression_commands` 单独写成纯文本命令文件；同时过滤空 / `None` / `none` failure layer，避免生成无效 `Repair None cases`。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_cli_writes_regression_command_file` 和 `test_evidence_dashboard_ignores_empty_failure_layers_for_repair_commands`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard `regression_commands` 现在会按 `by_permission` 自动生成 permission summary 精确查询，例如 `filesystem.write:granted` 和 `system.exec:granted`，使权限治理 evidence 可从看板直接进入对象级回归消费，而不是只依赖 `permission governance` 宽视图。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard `regression_commands` 现在会按 `by_status`、`by_category`、`by_requires` 和 `by_skip_reason` 自动生成 eval coverage 精确查询；已有 failed / skipped 固定视图会被去重，新增 passed、category、requires 和带空格 skip reason 的可复制查询命令。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard `regression_commands` 现在会按 `by_policy_reason` 生成 policy reason 精确查询，并按 `by_policy_context_budget` / `by_request_payload_stats` 中的高水位 `items:21+`、`json_chars:10000-49999` 和 `json_chars:50000+` bucket 生成热点查询，避免 context / payload 膨胀证据只能通过固定宽视图消费。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard `regression_commands` 现在会按 `by_skill` 生成 exact used skill 查询，例如 `builtin.data.config`、`generated.habit.*` 和 imported skill 名称，使 skill/tool regression 可从 runbook 直接进入具体 skill 维度，而不是只依赖 `skill_or_tool` failure layer 或 generated habit 宽视图。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard `regression_commands` 现在会按 `by_procedure` 和 `by_habit` 生成 exact procedure / habit 查询，例如 `--evidence-procedure proc_1` 和 `--evidence-habit habit_1`，使学习对象级回归可从 runbook 直接进入具体 procedure / habit evidence，而不是只依赖 learning kind、governance 或 lineage 宽视图。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard `regression_commands` 现在会按 `by_lineage_skill` 生成 exact lineage skill 查询，例如 `--evidence-lineage-skill generated.habit.demo`，使 skill lifecycle / promotion lineage 回归可直接进入具体 skill lineage evidence，而不是只依赖 generated habit skill usage 或 learning lineage 宽视图。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence index / dashboard 扩展记录：build index JSON `summary`、index Markdown、query summary 和 dashboard summary 现在输出 `by_scenario` / scenario 分布；dashboard `regression_commands` 会按 scenario id 生成 exact scenario 查询，例如 `--evidence-scenario learning_loop_case`，使 eval coverage 可从 index 到 runbook 直接进入具体场景回归。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_index_summarizes_runs_and_learning_artifacts`、`tests/test_real_world_eval_diagnostics.py::test_evidence_index_query_filters_and_summarizes_learning_lineage` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views`。

2026-06-13 `META-010` evidence dashboard 扩展记录：dashboard `regression_commands` 现在会为 failed cases 生成 `scenario_rerun` 命令，例如 `poetry run python scripts/run_real_world_evals.py --scenario config_case`；当失败 case 带 `requires=["complex"]` / `network` / `l4` 时，命令会自动补 `--include-complex` / `--include-network` / `--include-l4`，使 runbook 不只支持 evidence query，也能直接进入对应真实场景复跑。回归测试见 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_builds_common_runbook_views` 和 `tests/test_real_world_eval_diagnostics.py::test_evidence_dashboard_cli_writes_regression_command_file`。

2026-06-13 `META-010` skill promotion 扩展记录：file habit promoted skill 新增 `copy` contract，生成包自带 copy smoke test，并可在 runtime 中复制 workspace 内文件；仍只声明 `filesystem.read` / `filesystem.write`，不引入删除权限。回归测试见 `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_habit_promotes_to_active_skill_with_lineage_and_contract`。

2026-06-13 `META-010` skill promotion 扩展记录：file habit promoted skill 新增 `replace_line` contract，生成包自带 replace-line smoke test，并可在 runtime 中按唯一 `line_prefix` 替换 workspace 文本文件目标行；仍只声明 `filesystem.read` / `filesystem.write`，不引入删除权限。回归测试见 `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_habit_promotes_to_active_skill_with_lineage_and_contract`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 从 file 扩展到 JSON / JSONL data habit，生成 skill 包包含 `validate` / `query` / `query_lines` / `report_lines` / `format` / `report` contracts、测试和 lineage；后续扩展已支持 `jsonl` / `ndjson` target alias 归入 JSON promotion，并补充 JSONL line query、report 写入和真实评测 source target 查询验证；source habit 为 medium / high risk 时 validation 后不会自动审批或激活。回归测试见 `tests/test_skill_lifecycle.py` 和 `tests/test_execution_policy.py`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 CSV data habit，生成 skill 包包含 `read` / `select` / `filter` / `aggregate` / `report` / `write` contracts、测试和 lineage，并可执行 aggregate。后续扩展已支持 `tsv` target alias 归入 CSV promotion，并补充 tab delimiter aggregate 运行验证。回归测试见 `tests/test_skill_lifecycle.py`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 SQLite data habit，生成 skill 包包含 `list_tables` / `schema` / `query` / `export_csv` / `report` contracts、测试和 lineage，并可执行只读 query；生成 skill 使用 SQLite `mode=ro`、`PRAGMA query_only=ON` 和只读 SQL gate。回归测试见 `tests/test_skill_lifecycle.py`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 config habit，生成 skill 包包含 `query` / `report` contracts、测试和 lineage，并可执行 TOML config query。后续扩展已支持 `yaml` / `yml` / `toml` / `xml` / `dotenv` / `ini` / `properties` target alias 归入 config promotion，并补充 YAML、XML、dotenv、INI 和 properties query 运行验证。回归测试见 `tests/test_skill_lifecycle.py`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 plain text habit，生成 skill 包包含 `stats` / `extract_lines` / `replace` / `report` contracts、测试和 lineage，并可执行纯文本按行抽取。回归测试见 `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_text_habit_promotes_to_active_skill_and_executes_line_extract`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 markdown habit，生成 skill 包包含 `headings` / `extract_section` / `replace_section` / `append_section` / `table` contracts、测试和 lineage，并可执行 section extract。回归测试见 `tests/test_skill_lifecycle.py`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 HTML habit，生成 skill 包包含 `extract` / `links` / `report` contracts、测试和 lineage，并可执行 HTML summary report。回归测试见 `tests/test_skill_lifecycle.py`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 archive habit，生成 skill 包包含 `list` / `extract` / `create` contracts、测试和 lineage，并保留 extract traversal protection；可执行 workspace zip bundle 创建。后续扩展已支持 `gztar` / `tgz` / `tar.gz` target alias 归入 archive promotion，并补充 tar.gz 创建验证。回归测试见 `tests/test_skill_lifecycle.py`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 datetime habit，生成 skill 包包含 `now` / `parse` / `format` / `diff` / `add` contracts、测试和 lineage，不声明额外权限，并可执行 datetime format。回归测试见 `tests/test_skill_lifecycle.py`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 read-only Git habit，生成 skill 包包含 `status` / `diff` / `log` / `show` contracts、测试和 lineage；manifest 声明 `system.exec`、`git` dependency、`process_execution` 和 `process` sandbox，因此 validation 后停在 `validated`，只能人工 approval 后激活并执行。回归测试见 `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_git_habit_promotes_to_validated_skill_and_requires_manual_approval`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 Python habit，生成 skill 包包含 `run` contract、测试和 lineage；manifest 声明 `code_execution` permission 和 `python_execution` side effect，因此 validation 后停在 `validated`，只能人工 approval 后激活，并在 L3 `code_execution` 权限下执行 sandbox `run_python`。回归测试见 `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_python_habit_promotes_to_validated_skill_and_requires_manual_approval`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 HTTP habit，生成 skill 包包含 `get` / `post` / `put` / `delete` contracts、测试和 lineage；manifest 声明 `network` permission 和 `network_read` / `network_write` side effects，因此 validation 后停在 `validated`，只能人工 approval 后激活并在显式 `allow_network` 约束下执行。回归测试见 `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_http_habit_promotes_to_validated_skill_and_requires_manual_approval`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 browser fetch habit，生成 skill 包包含 `fetch` contract、测试和 lineage；manifest 声明 `network` permission 和 `network_read` side effect，因此 validation 后停在 `validated`，只能人工 approval 后激活并在显式 `allow_network` 约束下执行。生成 main 复用 sandbox `fetch_url`，并保留访问验证 / captcha / access denial 失败识别。回归测试见 `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_browser_habit_promotes_to_validated_skill_and_requires_manual_approval`。

2026-06-12 `META-010` skill promotion 扩展记录：habit -> skill promotion 新增 safe shell habit，生成 skill 包包含 `run` contract、测试和 lineage；manifest 声明 `system.exec`、`process_execution` 和 `process` sandbox，因此 validation 后停在 `validated`，只能人工 approval 后激活并执行。生成 main 只允许白名单读向命令、argv 和 `shell=False`。回归测试见 `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_shell_habit_promotes_to_validated_skill_and_requires_manual_approval`。

2026-06-12 `META-010` learning governance 记录：新增 `omniagent learning review` 统一只读审计入口，可跨 procedure / skill 查看 status、review_status、risk、findings、next_actions 和 evidence lineage。回归测试见 `tests/test_learning_governance.py`。

2026-06-12 `META-010` learning governance 扩展记录：`omniagent learning review --type experience` 可审计 workspace experience records，展示 validation verdict、failure layer、learning candidate count、artifact / validation evidence lineage，并对未通过 validation、非成功经验、缺少 evidence 或敏感原文存储标记 findings。回归测试见 `tests/test_learning_governance.py`。

2026-06-12 `META-010` learning governance 扩展记录：`omniagent learning review --type habit` 可将 habit 作为一等只读审计对象，展示 promotion candidate health、confirmation gate、validation evidence lineage 和 findings；`all` 视图也会将 habit 从普通 procedure 中拆出。回归测试见 `tests/test_learning_governance.py`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning show --type experience|procedure|habit|skill <id>` 单对象详情入口，返回统一审计 item 和原始对象 payload，便于从 review 列表追到具体 experience、procedure、habit 或 skill。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_show_outputs_experience_detail`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning edit --type procedure|habit <id>` 对象级编辑入口；该入口复用 procedure edit gate，返回 operation payload、previous_status 和 audit record，habit 行为字段编辑会降回 draft 并要求复审，不支持编辑只读 experience 或 skill 包。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_edit_controls_procedure_and_habit_objects`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning promote --type procedure|habit <id>` 对象级升级入口；`--type procedure` 复用 procedure -> habit promotion gate，`--type habit` 复用 habit -> skill promotion gate，并返回 target_type、target_id、validation / approval / registration evidence。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_promote_controls_procedure_and_habit_objects`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning decay --type procedure|habit <id>` 对象级衰减入口；procedure 会降回 draft review，habit 会降回 verified review，旧 success / failure 计数清零并写入 `decayed` audit event，必须带 fresh evidence refs approve 后才能清除 `decayed_requires_review` 并重新进入自动复用路径。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_decay_controls_procedure_and_habit_objects`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning request-changes --type procedure|habit <id>` 对象级要求修改入口；该入口复用 procedure `needs_changes` review gate，将 verified procedure / habit 降回 draft review，并进入统一 review / history 视图。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_request_changes_controls_procedure_and_habit_objects`。

2026-06-13 `META-010` learning governance 扩展记录：`omniagent learning validate` 已从 skill-only 扩展为 `--type procedure|habit|skill`；procedure / habit validate 复用 procedure review gate 和 evidence refs，skill validate 继续复用 lifecycle validation gate，返回 previous_status、review / validation evidence、approval policy、可选 approval record 和更新后的审计 item，不执行 skill activation。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_validate_controls_procedure_habit_and_skill_objects`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning disable --type procedure|habit|skill <id>` 对象级治理入口，统一禁用 procedure memory、habit fast-path candidate 和 skill learning object；procedure / habit disable audit note 保留 `previous_status`，被停用的 habit 可通过 `--include-disabled` 回到 habit 审计视图，skill disable 复用 lifecycle deprecate 并保留 registration note。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_disable_and_enable_controls_learning_objects`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning enable --type procedure|habit|skill <id>` 对象级恢复入口；procedure / habit 从最近一次 disable audit note 恢复到原状态，skill enable 复用 lifecycle activation gate 重新激活已 deprecated skill，并保留 registration note。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_disable_and_enable_controls_learning_objects`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning approve --type procedure|habit|skill <id>` 对象级审批入口；procedure / habit approval 复用 procedure review gate，skill approval 复用 lifecycle approval policy 并写 approval record，不自动绕过 activation gate。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_approve_controls_procedure_and_skill_objects`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning reject --type procedure|habit|skill <id>` 对象级拒绝入口；procedure / habit rejection 复用 procedure review gate，skill rejection 复用 lifecycle approval policy 并写 rejected approval record，rejected former habit 仍可通过 `--include-disabled` 回到 habit 审计视图。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_reject_controls_procedure_habit_and_skill_objects`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning rollback --type skill <skill_name> --to-version <version>` 对象级回滚入口；该入口复用 skill lifecycle rollback gate，返回 previous_version、target_version、registration record 和回滚后的 skill detail。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_rollback_controls_skill_object`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning history --type experience|procedure|habit|skill <id>` 对象级历史入口；该入口统一聚合 experience recorded / validated、procedure audit / review 和 skill draft / validation / approval / registration / lineage 事件，作为 review/show 之外的生命周期审计视图。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_history_outputs_audit_events`。

2026-06-12 `META-010` learning governance 扩展记录：新增 `omniagent learning explain-match <task_or_goal>` 统一匹配解释入口；该入口复用 procedure / habit retrieval，返回 matched audit item、rejected candidate items、confirmation requirement 和 suggestion mode，让学习对象从“可审计”推进到“为什么被复用 / 为什么被拒绝”可审计。回归测试见 `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_explain_match_outputs_habit_audit_context`。

2026-06-12 `META-010` learning gate 收口记录：补齐“学习发生在验证之后”的回归证据；成功但未验证的 `ExperienceRecord` 不生成 `procedure_candidate`，`not_verified` / `partial` / `failed` 或缺少 evidence 的 experience 不能提炼 `ProcedureDraft`。回归测试见 `tests/test_experience_record.py::ExperienceRecordIntegrationTest::test_success_without_validation_does_not_create_procedure_candidate` 和 `tests/test_procedure_memory.py::ProcedureMemoryStoreTest::test_extract_draft_requires_passed_validation_with_evidence`。

2026-06-12 `META-010` permission governance 收口记录：补齐高风险 habit 在真实 runtime 路径中的回归证据；即使高风险 habit 已匹配，`habit_fast_path.json` 仍为 `planner_required`、`skipped_stages=[]`，`policy_decision.json` 不会接受 fast path。回归测试见 `tests/test_procedure_memory.py::ProcedureMemoryRuntimeIntegrationTest::test_runtime_high_risk_habit_requires_confirmation_and_no_fast_path`。

### ✅ 已关闭 ISSUE

| ID | 一句话 | 状态 | 备注 |
|---|---|---|---|
| BASECAP-002 | 标准 skill 库 v1 首批能力缺口 | ✅ 完成 | 新增 `builtin.data.sqlite` / `builtin.text.markdown` / `builtin.vcs.git` / `builtin.web.html` / `builtin.archive.basic` / `builtin.data.config` |
| ISSUE-007 | skill_registry_entries 全量发送 | ✅ 完成 | 已在 `BASECAP-001` 阶段 4 完成 |
| ISSUE-015 | Browser executor 中文 URL UnicodeEncodeError | ✅ 完成 | 已在 `BASECAP-001` 阶段 0 完成 |
| REAL-EVAL-001 | 默认真实场景评测首轮失败 | ✅ 完成 | 2026-05-11 修复 CSV/JSON report、权限静态分析、网络拒绝审计和评测进度输出 |
| REAL-EVAL-002 | 多轮对话中 `status:` 字段误触发状态查询 | ✅ 完成 | 2026-05-11 扩充 conversation 场景并收窄 router 状态查询规则 |
| REAL-EVAL-003 | 真实评测缺少结构化诊断报告 | ✅ 完成 | 2026-05-11 每个 case 生成 `diagnostics.md`，并新增 `failure_layer`、turn trace 和 workspace 产物快照 |
| REAL-EVAL-004 | 控制流节点继承 preferred skill 的高权限 | ✅ 完成 | 2026-05-11 全量诊断发现 `finalize` 节点错误携带 network / system.exec；已改为控制流节点不参与 skill 选择权限继承 |
| REAL-EVAL-005 | 第二批真实场景矩阵：错误恢复、复杂多轮、规划合理性和产物质量 | ✅ 完成 | 2026-05-12 新增 10 个场景与断言能力；最终 `eval_runs/20260512_032854/` 为 10 passed / 0 failed / 0 skipped；本地 `377 passed`，ruff 通过 |
| ISSUE-017 | 上下文召回冗余与陈旧运行态污染 Planner / Critic | ✅ 完成 | 2026-05-20 修复失败 workflow 正向召回、vector embedding 入 prompt、memory/workflow/vector 跨通道重复、依赖输出和 Critic payload 过大、陈旧 planning/running 任务状态；新增同类回归与真实评测场景 |
| ISSUE-018 | `builtin.data.config` 对 TOML section query 过严导致配置报告场景失败 | ✅ 完成 | 2026-05-20 `config_toml_report` 暴露 Planner 使用 `service_name` / `owner` 等报表字段作为 query；已支持无歧义 alias 并补充契约文档和回归测试；复跑 `eval_runs/20260520_144321/` 通过 |

---

## ✅ BASECAP-001：基础能力升级

**症结**：专项启动时 runtime 闭环已经可用，但标准 builtin skill 层太薄，冷启动能力主要依赖 `file` / `python` / `browser` 三个基础包装；这会限制 Planner 的可选能力，也会把高频任务推向自动生成 skill 或临时 Python。

**完成内容**：

1. 阶段 0：修 Browser 中文 URL、配置安全、builtin skill 规范（已完成）
2. 阶段 1：落地 `builtin.data.json` / `builtin.datetime.format` / `builtin.data.csv`（已完成）
3. 阶段 2：落地 `builtin.http.client`（已完成）
4. 阶段 3：落地 `builtin.shell.safe`（已完成）
5. 阶段 4：再做 skill 选择质量升级和 `ISSUE-007`（已完成）

**完成记录**：见 [`BASIC_CAPABILITIES_UPGRADE.md`](./docs/archive/pre-product-redesign/BASIC_CAPABILITIES_UPGRADE.md)。该项已关闭，不再作为当前阻塞项。

---

## 🔴 CAP-EVAL-001：复杂能力评测前置升级

**症结**：继续扩充复杂真实场景前，当前评测体系无法稳定区分模型能力不足、Agent 流程问题、工具/skill 问题、记忆召回问题和测试预期问题。任务级 completion evaluation、真实 embedding 与 diagnostics 归因证据已落地，下一步用复杂场景验证这些能力。

**目标**：

1. 新增 completion evaluation artifact，明确判断任务是否真正满足用户原始目标。
2. 在 diagnostics 中增加可审计的失败归因证据。
3. 引入 `model_capability` 归因，避免把模型能力边界误修成 Agent patch。
4. 接入 OpenAI-compatible 与进程内 local_model 真实 embedding，并保留 hash fallback。（已完成）
5. 完成后再启动 `REAL-EVAL-006` 复杂真实场景矩阵。

**状态**：✅ 完成。阶段 0 / 阶段 1 / 阶段 2 / 阶段 3 / 阶段 4 已归档，任务文档见 [`COMPLEX_CAPABILITY_EVAL_UPGRADE.md`](./docs/archive/pre-product-redesign/COMPLEX_CAPABILITY_EVAL_UPGRADE.md)。

**当前验证**：`poetry run python -m pytest tests/` 为 545 passed，`poetry run ruff check src/ tests/ scripts/run_real_world_evals.py` 通过，`poetry run python scripts/run_real_world_evals.py --check-only` 校验 41 个场景通过。

**阶段 4 启动记录**：`REAL-EVAL-006` 首批 3 个复杂场景已加入并默认通过 `requires: ["complex"]` 跳过；runner 新增 `--include-complex`、`setup_vector_memory` 和 vector recall 断言。当前增量验证：`python3 scripts/run_real_world_evals.py --check-only` 为 23 scenarios validated，`poetry run pytest tests/test_real_world_eval_diagnostics.py` 为 10 passed，相关 `ruff check` 通过。

**阶段 4 首轮通过记录**：2026-05-21 新增 5 个复杂专项场景完整回归 `eval_runs/20260520_165848/` 为 5 passed / 0 failed / 0 skipped。同步新增确定性 Planner 快速路径：显式 `executor=file` + `payload` 使用 payload 作为权威工具输入；`builtin.data.config` 明确报告任务可直接生成 config report 计划，降低真实 provider 超时对能力评测的污染。

**阶段 4 最终归档记录**：2026-05-21 `eval_runs/20260521_073254/` 默认非 Git 基线 27 passed / 0 failed / 11 skipped；`eval_runs/20260521_092801/` 复杂矩阵 8 passed / 0 failed / 0 skipped；当时 `poetry run python -m pytest tests/` 为 456 passed，`poetry run ruff check src/ tests/ scripts/run_real_world_evals.py` 通过，`python3 scripts/run_real_world_evals.py --check-only` 校验 38 个场景通过。当前 META-009 首轮后回归基线已更新为 463 passed / 39 scenarios validated。

**暂停范围**：P1 流式输出、P2 其他智能深化、P3 生产化和长期认知抽象方向，除非是阻塞本任务或用户明确指定。

---

## 🔴 META-002：路由层本体（ontology）混乱

**症结**：`UserInputType` 枚举把管线指令 / 信息查询 / 生命周期回应 / catch-all 塞同一个层级，rule-based 路由靠手工关键词集合分流。

**修复方向**：
1. 拆本体为多维标注：`InputClassification(intent_kind, references, requires_planning)`
2. Rule-based 只保留 fast path（`/help`、明确 approval、明确 status）
3. 关键词只用于短路，语义分类一律 LLM router
4. LLM router 附 confidence，runtime 按 confidence 做缓存

**当前缓解**：`SMALL_TALK` 和 chitchat 关键词列表已随 META-004 删除，`_rule_based` 末尾直接落 NEW_TASK 不再调 LLM 兜底。2026-05-11 已收窄 `status` 关键词规则，避免文件字段 `status:` 误触发状态查询。2026-05-21 补充 summarize/report 等任务型信号，避免“status question”作为上下文描述时误触发状态查询。完整拆分仍待。

---

## 🔴 META-005：失败不反向传播（无 closed-loop learning）

**症结**：执行失败的信息只在执行层内流转，不回到选择/路由/规划层影响下次决策。

**修复方向**：
1. 每次决策（route / plan / dispatch）写 `decision_record`
2. 下次同类决策把 records 作 negative signal
3. decision_records 进 graph_memory，跨 task 学习

**当前缓解**：critic fail verdict 强制执行 + score threshold 间接减少了重复失败的影响，但不是 closed-loop。

---

## 🔴 META-010：Agent usability and learning loop MVP

**症结**：当前实现更像 task-centric runtime，用户可感知的“聪明”和“学习”不足。记忆主要是 recall，workflow / skill lifecycle 偏工程沉淀，重复任务没有形成稳定的 experience -> procedure -> habit -> skill 升级路径。

**目标**：按最新产品架构重开发为 learning-loop-centered personal Agent OS。首期先证明 Agent state、ExperienceRecord、Procedure memory、Habit fast path、Skill promotion 这条闭环。

**修复方向**：

1. 新增 Agent state：`AgentProfile`、`UserPreference`、`ProjectState`、`CurrentGoal`。
2. 每次任务结束后写结构化 `ExperienceRecord`，成功、失败、权限拒绝都要有 evidence。
3. 从 experience 提炼 `ProcedureDraft`，下次相似任务先检索 procedure。
4. 只有 verified procedure 能升级为 habit；habit fast path 仍保留 validator 和 permission gate。（已完成）
5. 只有稳定 habit 才能进入 skill promotion，skill 必须带 contract、测试或验证脚本和 evidence lineage。（已完成）

**边界**：不在首期做多入口 UI，不重写所有 executor，不把未验证学习结果自动用于高风险行为。

**专项文档**：[`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](./docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md)。

---

## 📦 META-009：Runtime 分层执行与性能优化（已归档）

**症结**：Runtime 目前默认把任务统一送入 `Planner -> Execution -> Critic -> Completion`。真实场景 `context_recall_hygiene_filters_failed_and_duplicate_memory` 显示，一个低风险结构化 file write 任务耗时 `104.448s`，其中 Planner / Critic / Completion 三次 LLM 调用合计 `28,906` tokens；Planner request payload 约 `29.9KB`，其中 `skill_registry_entries` 约 `26.7KB`。

**目标**：建立按任务确定性、风险和输入结构分层的执行策略。低风险、结构化、payload 完整的任务可走 fast path；自然语言、多步、指代、网络、L4、删除、失败恢复等场景仍走完整 Planner 和验证链路。

**修复方向**：

1. 新增 `ExecutionPolicy` / `PlanningPolicy`，在 Planner 调用前判断 `direct_execute` / `plan_required` / `plan_with_compact_registry`。
2. 第一阶段只允许显式 `executor + payload + L2 以内 + 无上下文指代` 的任务跳过 Planner。
3. Planner 仍需参与时，默认只发送 `SkillCapabilityIndex`；完整 skill manifest / contract 仅在显式指定、action 精确匹配、contract gap 或 replan 时按需展开。
4. 后续再条件化 task-level Critic / Completion，并以本地 artifact validator 替代部分低风险成功路径验证。
5. 每次策略决策必须写入 artifact 和 diagnostics，包含跳过原因、上下文字段大小和回退路径。

**边界**：不全局关闭 Planner，不削弱 permission / sandbox，不跳过高风险或复杂任务验证，不为了速度降低评测断言。

**专项文档**：[`RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md`](./docs/archive/pre-product-redesign/RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md)。

---

## 🟡 ISSUE-003：分层召回（按置信度路由结果）

**背景**：所有召回结果一视同仁塞进 LLM context，无论 confidence 高低。前置依赖（LLM 可观测性 + Recommendation 多维评分）已全部落地。

**方案**：按阈值分层 — 高置信(>0.8)进 Planner 主 context，中置信(0.5-0.8)进 Critic 兜底，低置信(<0.5)只记 explanation。

---

## 🟡 ISSUE-004：Token 超限无裁剪策略

**背景**：request_payload 超 context window 直接 HTTP 400，无降级。

**方案**：`_trim_payload_if_needed` 按优先级裁剪（bootstrap → 召回 → 次要召回），裁剪时在 metadata 记录。当前 Qwen 32k context 不太触发，触发后再做。

---

## ✅ ISSUE-017：上下文召回冗余与陈旧运行态污染

**背景**：真实 workspace 记录显示 Planner 上下文存在多类冗余：失败 workflow 被当作正向 workflow 召回、vector match 携带 512 维 embedding 原文、同一 task 同时出现在 memory/workflow/vector 三个通道、依赖节点输出同时传 `body` 与 `body_preview`、Critic 请求体未压缩、旧 `planning` / `running` 任务长期停留在活跃状态。

**状态**：已完成。修复落点为公共上下文入口而非单场景补丁：

1. 文件 workflow 召回只保留 completed 记录。
2. vector recall 返回 prompt-safe 形状，去除 raw embedding，并过滤失败状态。
3. Hybrid/runtime 对 workflow、memory、vector 做跨通道去重。
4. ExecutionEngine 注入依赖输出前压缩大文本并避免 `body` / `body_preview` 双份传递。
5. Critic 对 node、execution_result、召回证据做递归 compact。
6. LLM call 日志新增 `request_payload_stats`，Planner 日志记录完整请求 payload 形状。
7. Gateway 在对话和状态查询前把超时未更新的 transient task 标记为 `failed/stale_task_reconciled`。

**验证**：新增 `tests/test_context_management.py` 和依赖输出回归，覆盖实际暴露的同类路径。新增真实场景 `context_recall_hygiene_filters_failed_and_duplicate_memory`，`eval_runs/20260520_105058/` 为 1 passed / 0 failed。

---

## ✅ ISSUE-007：skill_registry_entries 全量发送

**背景**：Planner 曾经每次收到所有 active skill manifest。结构化 SkillContract 已落地，可按 `(action, input_schema)` 过滤。专项启动时 skill 数少，不是瓶颈。

**状态**：已完成。Planner 入口现在按 action / tags / executor_hint / 显式 skill / 权限适配 / 成功失败历史预过滤并限制 manifest 数量；Registry 选择加入 action 精确匹配、权限等级、成功率和失败次数权重；contract / permission 负信号写入 graph memory。

---

## ✅ ISSUE-015：Browser executor 中文 URL UnicodeEncodeError

**背景**：browser skill 处理含中文字符的 URL 时抛 `UnicodeEncodeError`。thread_3f7a64b4cb 中 replan 后的 browser 节点尝试访问含中文的 wttr.in URL 失败。

**状态**：已完成。`sandbox.fetch_url()` 会在 HTTP/HTTPS 请求前对 path / query / fragment 的 non-ASCII 部分做安全百分号编码，并保持 ASCII URL 行为不变。
