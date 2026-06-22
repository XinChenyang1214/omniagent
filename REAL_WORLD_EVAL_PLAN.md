# 真实场景自主评测计划

## 目标

把“真实测试 Agent 能力”变成可重复的工程闭环：

```text
用户只定义目标
  -> Codex 设计场景
  -> 自动运行隔离评测
  -> 读取 CLI 输出、任务产物、LLM 调用、节点与权限日志
  -> 归因失败层级
  -> 修改代码 / prompt / 文档 / 测试
  -> 再跑评测确认
```

这不是替代单元测试，而是覆盖真实 LLM、Planner、Skill 选择、权限、Executor、Critic、Responder 的端到端验收。

## 当前优先级状态（2026-06-12）

`REAL-EVAL-005` 第二批真实场景矩阵已完成。`CAP-EVAL-001` 阶段 0-4 已完成，`REAL-EVAL-006` 复杂真实场景矩阵已归档为回归集。

前置原因：

- 任务完成度评估节点已落地，runner diagnostics 已汇总该结果。
- failure attribution 已能消费 completion evaluation 的 `primary_failure_layer` 和 `model_capability_suspected`。
- vector recall 已支持 OpenAI-compatible 与进程内 local_model 真实 embedding，runner diagnostics 已汇总 provider/model/dimensions/fingerprint/fallback 和召回兼容性证据。

`REAL-EVAL-006` 已覆盖复杂语义召回、多轮复杂指代、模型能力边界和 completion evaluation 与实际产物不一致的场景。

当前真实评测基线已归档，首轮新增复杂专项场景已真实跑通：

- 已归档基线场景数：33。
- 已归档非 Git 场景：32/32 已有通过记录。
- 已归档 Git 场景：`git_status_readonly_report` 按用户要求未执行，不纳入本次归档通过口径。
- 当前场景文件总数：58；默认非 Git 基线 `eval_runs/20260521_073254/` 为 27 passed / 0 failed / 11 skipped；复杂矩阵 `eval_runs/20260521_092801/` 为 8 passed / 0 failed / 0 skipped；learning-loop 场景 `eval_runs/20260612_034828/` 和 `eval_runs/20260613_042808/` 均为 1 passed / 0 failed / 0 skipped；首批 `general_agent_usability_*` 场景已加入并通过 `--check-only`，覆盖 5 类学习序列、5 个自然语言 tool-choice 场景和 5 个权限边界场景，离线 `OMNIAGENT_LLM_PROVIDER=test` 最新基线已写入 `eval_runs/general_agent_usability_offline_latest/`，真实 LLM provider 基线仍待建立；`git_status_readonly_report` 仍按用户要求未执行。
- 跨 eval evidence index 已可通过 `poetry run python scripts/run_real_world_evals.py --build-evidence-index` 生成；当前本地索引写入 `eval_runs/evidence_index.json` / `eval_runs/evidence_index.md`，覆盖 67 个 run、463 个 case，并在 JSON `summary` 与 Markdown summary 中展示 status / scenario / category / requires / skip reason / failure layer 分布。
- evidence index 可通过 `poetry run python scripts/run_real_world_evals.py --query-evidence-index` 查询，支持按 failure layer、used skill、procedure id、habit id、skill lineage、scenario id、promotion generation type、promotion source target、category、requires、skip reason、permission summary、completion evaluation failure layer、ExecutionPolicy policy decision、ExecutionPolicy skipped stage、ExecutionPolicy risk flag、ExecutionPolicy reason、ExecutionPolicy context budget、LLM request payload stats、TaskGraph node status、Critic action、memory retrieval / embedding signal、LLM usage / token / duration signal、learning evidence kind、learning validation evidence、skill validation status、procedure / habit review decision 和 audit event 过滤，并可输出 Markdown / JSON 回归报告视图。
- evidence dashboard 可通过 `poetry run python scripts/run_real_world_evals.py --evidence-dashboard` 生成，默认写入 `eval_runs/evidence_dashboard.md`，固定包含失败修复入口、regression commands（含 failed scenario rerun 命令、固定视图查询、eval coverage status / scenario / category / requires / skip reason 动态查询、used skill 动态查询、procedure id 动态查询、habit id 动态查询、lineage skill 动态查询、promotion generation type 动态查询、promotion source target 动态查询、permission summary 动态查询、completion layer 非 `none` 动态查询、policy decision 动态查询、skipped stage 动态查询、policy risk flag 动态查询、policy reason 动态查询、policy context budget / request payload stats 高水位动态查询、node status `status:*` / `attempts:*` / `error:*` 动态查询、critic action `action:*` 动态查询、memory retrieval 低基数状态动态查询、LLM usage 低基数 token / duration / call-state 动态查询、learning kind 动态查询、learning validation 动态查询、skill validation 动态查询、learning review decision 动态查询和 learning audit event 动态查询）、eval coverage（status / scenario / category / requires / skip reason）、skipped eval、failure layer 查询命令、skill/tool 失败、model capability、node execution、critic action、completion evaluation、memory retrieval、LLM usage、runtime cost hotspot、execution policy、Planner skipped、policy risk flag、policy reason、policy context budget、request payload stats、permission governance、habit fast path、learning match explanation、learning gate、learning governance、learning history、learning approval、learning promotion、learning validation、learning edit、learning request-changes、learning decay、learning disable、learning restore、learning rollback、learning rejection、learning audit gap、generated habit skill 使用、skill lifecycle governance、skill promotion coverage、skill approval gate 和 learning lineage cases；需要单独导出可复制命令清单时，使用 `--evidence-dashboard-commands-output eval_runs/evidence_dashboard.commands`。
- 新增 8 个 `requires: ["complex"]` 的复杂场景已通过完整矩阵回归。
- 最近关键证据：
  - `eval_runs/20260520_105058/`：上下文召回卫生场景通过。
  - `eval_runs/20260520_134202/`：首批 3 个复杂场景通过。
  - `eval_runs/20260520_143007/`：剩余 5 个非 Git 基础能力场景初跑，4 passed / 1 failed。
  - `eval_runs/20260520_144321/`：修复 `builtin.data.config` TOML section query alias 后，`config_toml_report` 复跑通过。
  - `eval_runs/20260520_165848/`：新增 5 个复杂专项场景完整回归通过。
  - `eval_runs/20260612_034828/`：`META-010` learning-loop habit-promoted skill lineage 场景通过。
  - `eval_runs/20260613_042808/`：`META-010` JSONL habit-promoted skill lineage 场景通过，policy artifact 为 `direct_execute`，`skipped_stages=["planner"]`，并可通过 `--evidence-promotion-source-target jsonl` 从 evidence index 查询。
- `eval_runs/20260521_073254/`：默认非 Git 基线最终通过。
- `eval_runs/20260521_092801/`：8 个复杂场景最终通过。
- `eval_runs/policy_explicit_check/20260521_104629/`：META-009 显式 payload fast path 通过，diagnostics 显示 `direct_execute` 且无 Planner LLM 调用。
- `eval_runs/20260521_104512/`：Planner context 验收场景通过，策略层因 `planner_context_required` 回退完整 Planner。
- `eval_runs/policy_complex_context_check/20260521_104738/`：复杂 context 预算场景通过，策略层保留 Planner context 验收。

该归档表示旧的非 Git 基线和首轮复杂专项场景可作为回归集使用，不表示可以删除场景定义或跳过后续回归。

性能与成本维度也需要持续记录。`META-009` Runtime 分层执行与性能优化已归档为旧实现迁移输入；`META-010` 期间真实评测应继续关注 runtime 成本，同时新增 learning-loop 证据：

- 每个场景端到端 `duration_seconds`
- Planner / Critic / Completion 的 tokens 与 latency
- `request_payload_stats` 中 `skill_registry_entries`、召回字段和 bootstrap 字段大小
- 执行前 `TaskIntakeAssessment` 是否正确记录任务类型、风险、工具、验证方式和确认需求
- fast path 是否触发、为什么触发或为什么回退完整 Planner
- 跳过 Critic / Completion 时是否有本地 artifact 证据替代
- 是否生成 `ExperienceRecord`
- 是否命中或拒绝 procedure / habit
- 学习升级是否引用了 validation evidence
- 跨 run evidence index 是否能串起 case、scenario、status / category / requires / skip reason、diagnostics、failure layer、skill usage、node status、critic action、policy decision、skipped stages、risk flags、policy reasons、policy context budget、request payload stats、LLM calls / tokens / duration、procedure / habit artifacts 和 skill lineage

复杂场景默认跳过，需要显式开启：

```bash
poetry run python scripts/run_real_world_evals.py --include-complex
```

复杂场景中的 `local_model` embedding 断言需要使用 Poetry 环境运行。若用系统 `python3` 直接执行，可能因为缺少 `onnxruntime` / `tokenizers` / `numpy` 而降级到 `local_hash` fallback，导致评测结论失真。

首批 `REAL-EVAL-006` 场景：

- `context_recall_hygiene_filters_failed_and_duplicate_memory`：验证 Planner 上下文里失败 workflow 不作为正向召回、workflow/vector 跨通道去重、vector match 不携带 raw embedding，并记录 request payload stats。
- `semantic_memory_recall_incident_brief`：验证隔离 workspace 内的 `setup_vector_memory`、local_model embedding、compatible vector match 和诊断证据。
- `chat_nested_reference_revision`：验证多文件、多轮、嵌套指代和“刚修改的对象”引用。
- `completion_detects_incomplete_requirements`：验证 completion evaluation 对多必填字段产物的辅助诊断。

新增复杂专项场景：

- `context_budget_many_memories_keeps_relevant_matches`：验证大量 workflow/vector 记忆下仍保留相关召回、排除失败记忆，并限制 request payload 字段大小。
- `chat_reference_survives_status_and_correction`：验证多轮文件指代在插入状态查询后仍指向最近修改对象。
- `config_missing_nested_key_fails_with_tool_layer`：验证配置缺失必填 key 时，失败归因落在 skill/tool 层，且不生成伪造报告。
- `archive_extract_rejects_traversal_entry`：验证 archive extract 对路径穿越条目失败，且不释放 unsafe entry。
- `completion_overrides_successful_wrong_file_payload`：验证节点执行成功但产物不满足原始目标时，completion evaluation 能覆盖成功结果。

新增复杂专项场景最终验证：

- 初跑 `eval_runs/20260520_160630/`：2 passed / 3 failed，暴露状态查询误路由、config 缺失 key 断言口径、显式 file payload 被 Planner 改写和 completion evaluator 证据不足。
- 修复后完整回归 `eval_runs/20260520_165848/`：5 passed / 0 failed / 0 skipped。
- 修复落点：状态查询规则补充任务型 summarize/report 信号；显式 `executor=file` + `payload` 走确定性计划；`builtin.data.config` 明确报告任务走确定性 config report 计划；completion evaluator 纳入节点 `input_payload` 并归一化 `model_capability_suspected`。

`CAP-EVAL-001` 最终归档验证：

- 默认非 Git 基线：`eval_runs/20260521_073254/`，27 passed / 0 failed / 11 skipped。
- 复杂矩阵：`eval_runs/20260521_092801/`，8 passed / 0 failed / 0 skipped。
- 定向修复验证：
  - `eval_runs/20260521_062408/`：文件读回、无效 JSON、状态后继续、跨文件引用 4 passed。
  - `eval_runs/20260521_072326/`：Critic provider 波动、显式 browser skill payload、上下文召回卫生 5 passed。
  - `eval_runs/20260521_090126/`：状态查询后的复杂引用持久性场景通过。
  - `eval_runs/20260521_092604/`：语义记忆召回和发布就绪报告 2 passed。
- 本轮修复落点：
  - `builtin.file.basic` 和 file executor append 自动补分隔换行，避免追加内容粘连。
  - Planner 增加保守确定性计划：网络权限拒绝、JSON/config report、显式 builtin skill payload、明确 one-line/multi-line/field report 文件写入、读源文件后写报告、替换行并追加内容、first/second 历史文件指代。
  - `RuntimeGateway` 的 continue fork 使用本轮 continuation 作为新 goal / expected_output，避免完成度评估继续用旧任务目标。
- task-level Critic LLM 调用失败时降级为低置信度 fallback verdict，保留已观察到的工具执行结果和 CLI JSON 输出。

`META-009` 首轮真实评测记录：

- 新增 `explicit_file_payload_fast_path`，验证低风险显式 `executor=file` + payload 进入 `ExecutionPolicy.direct_execute`。
- runner diagnostics 新增 `Execution Policy` 区块，汇总 `policy_decision`、`skipped_stages`、`risk_flags`、`context_budget`。
- 当目标或 expected output 明确要求 Planner context / planning context 时，策略层回退 `plan_required`，避免跳过真实评测的验收对象。
- 验证结果：`eval_runs/policy_explicit_check/20260521_104629/`、`eval_runs/20260521_104512/`、`eval_runs/policy_complex_context_check/20260521_104738/` 均通过。
- 一次默认基线 `eval_runs/20260521_100101/` 因旧 context 场景断言与 direct execute 新语义冲突中止，不作为通过基线；修复后用上述定向场景复核。

## 当前评测入口

场景文件：

- [`evals/real_world/scenarios.json`](./evals/real_world/scenarios.json)

运行脚本：

- [`scripts/run_real_world_evals.py`](./scripts/run_real_world_evals.py)

默认只运行本地低风险场景，不跑网络，不跑 L4 shell：

```bash
poetry run python scripts/run_real_world_evals.py
```

只列出场景：

```bash
poetry run python scripts/run_real_world_evals.py --list
```

校验场景文件：

```bash
poetry run python scripts/run_real_world_evals.py --check-only
```

包含网络场景：

```bash
poetry run python scripts/run_real_world_evals.py --include-network
```

包含 L4 safe shell 场景：

```bash
poetry run python scripts/run_real_world_evals.py --include-l4
```

运行单个场景：

```bash
poetry run python scripts/run_real_world_evals.py --scenario csv_sales_summary
```

从已有 `eval_runs/` 生成跨 run evidence index，不重新执行评测：

```bash
poetry run python scripts/run_real_world_evals.py --build-evidence-index
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage llm_called
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-llm-usage "tokens:10000+,duration:60s+"
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-promotion-generation-type "json"
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-promotion-source-target "tsv"
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-decision direct_execute
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-skipped-stage planner
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-risk-flag planner_context_required
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-reason "*风险标记*"
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-policy-context-budget "skill_registry_entries:json_chars:50000+"
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-request-payload-stats "skill_registry_entries:json_chars:10000-49999"
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-node-status status:failed
poetry run python scripts/run_real_world_evals.py --query-evidence-index --evidence-critic-action action:accept
poetry run python scripts/run_real_world_evals.py --evidence-dashboard
poetry run python scripts/run_real_world_evals.py --evidence-dashboard --evidence-dashboard-commands-output eval_runs/evidence_dashboard.commands
```

## 隔离策略

每次运行都会生成独立目录：

```text
eval_runs/<run_id>/
├── cases/<scenario_id>/
│   ├── diagnostics.md
│   ├── result.json
│   ├── turn_01_command.json
│   ├── turn_01_stdout.txt
│   ├── turn_01_stderr.txt
│   └── turn_01_parsed.json
├── data/<scenario_id>.sqlite3
├── logs/<scenario_id>.log
├── workspaces/<scenario_id>/
│   ├── tasks/
│   ├── threads/
│   ├── MEMORY/
│   └── ...
├── summary.json
└── summary.md
```

脚本会为每个场景设置：

- 独立 `--workspace`
- 独立 `OMNIAGENT_SQLITE_PATH`
- 独立 `OMNIAGENT_LOG_PATH`
- workspace 授权路径

这样真实测试不会污染主 `workspace/` 和主数据库。

## 场景分类

当前内置场景覆盖：

- `filesystem`：文件创建、读取、内容校验
- `data`：CSV / JSON builtin skill 使用与结果校验
- `permission`：网络未授权时必须拒绝
- `conversation`：多轮对话和上下文延续
- `network`：真实 HTTP 场景，默认跳过
- `shell`：L4 safe shell 场景，默认跳过

## 失败归因

当前脚本会把失败粗分为：

- `cli_or_infrastructure`：CLI 未返回 JSON、进程错误、配置错误
- `runtime_timeout`：场景超时
- `router_or_interaction_dispatch`：对话路由或 interaction dispatch 把任务意图派错路径
- `planner_or_skill_selection`：结果可运行但未使用期望 skill
- `permission_guardrail`：不该授予的权限被授予
- `permission_detection`：期望看到权限拒绝，但日志里没有
- `permission_policy_or_scenario_config`：权限被拒，需判断是策略正确还是场景配置错
- `executor_or_skill_contract`：节点失败，优先查 executor / skill contract
- `task_outcome_mismatch`：任务完成但产物不符合预期
- `assertion_failure`：其他断言失败

`CAP-EVAL-001` 完成后，失败归因需要升级为能额外识别：

- `model_capability`：上下文、工具结果和 schema 均充分，但模型仍无法完成推理或判断
- `memory_retrieval`：真实 embedding、召回命中或新旧向量兼容问题
- `completion_evaluation`：任务完成度评估节点自身误判或证据不足
- `test_expectation`：场景断言或预期与产品目标不一致

每个已执行场景都会额外生成 `cases/<scenario_id>/diagnostics.md`，作为首选排查入口。它会汇总：

- 场景目标、轮次输入和 CLI 命令输出位置
- route / thread / task trace
- 使用的 skill、权限决策、节点状态、critic verdict、LLM 调用摘要
- workspace 用户产物快照
- `diagnosis` 之外的 `failure_layer`，用于决定先修 router、planner、permission、skill/tool、task flow/output、assertion 还是 infrastructure

## Codex 工作方式

当用户给出目标后，Codex 应按这个顺序执行：

1. 扩展或选择真实场景
2. 运行 `scripts/run_real_world_evals.py`
3. 阅读 `eval_runs/<run_id>/summary.md`
4. 对失败场景先阅读 `cases/<scenario_id>/diagnostics.md`
5. 必要时阅读 `cases/<scenario_id>/result.json`
6. 继续查对应 workspace 下的 `tasks/<task_id>/plan/`、`nodes/`、`critic/`、`llm_calls/`
7. 判断失败层级
8. 判断问题是否反映架构边界、模块职责或执行流程的不合理，而不只看当前报错
9. 选择正确修复层级：测试断言、prompt、skill contract、权限策略、dispatcher、executor、critic 或更上层流程
10. 做最小但正确的修复
11. 跑相关单元测试和对应真实场景
12. 更新 `issue.md` / `ROADMAP.md`，只记录真实暴露的问题和已完成修复

如果同类失败可能在多个场景重复出现，应优先沉淀为 `issue.md` 中的 META / ISSUE，再决定实现修复；不要为了让单个场景通过而绕开系统设计。

复杂场景评测还必须额外判断：失败是否可能是模型能力不足导致。如果证据显示 Agent 已提供充分上下文、工具结果和 schema，且流程本身合理，应记录为 `model_capability`，不要为了迁就单个模型做不合理的 Agent 结构补丁。

## 安全边界

- 默认不运行网络场景
- 默认不运行 L4 shell 场景
- 不使用真实账号、真实生产数据或真实密钥作为测试输入
- 删除、发布、外部系统写入、付费接口调用仍需用户明确确认
- 真实评测可以消耗 LLM token；运行前应确认当前 provider 配置指向预期环境

## 当前基线

2026-05-11 已完成默认真实场景回归：

- 运行目录：`eval_runs/20260511_073955/`
- 结果：5 passed / 0 failed / 2 skipped
- 跳过项：`http_example_status` 需要 `--include-network`；`safe_shell_wc` 需要 `--include-l4`
- 本轮暴露并修复：CSV / JSON 简单数据报告不应退回 Python；Python 权限静态分析不应把普通标识符误判为依赖安装；未授权网络请求应由权限层产生可审计 denial；verbose 评测应在场景开始时输出进度。

2026-05-11 已补跑扩展场景：

- 运行目录：`eval_runs/20260511_081341/`
- 结果：2 passed / 0 failed / 0 skipped
- 覆盖：真实 HTTP `builtin.http.client` + L4 `builtin.shell.safe`
- 证据：HTTP 场景写出 `http_status: 200`；safe shell 场景写出 `line_count: 4`

2026-05-11 已扩充并重跑多轮对话场景：

- 运行目录：`eval_runs/20260511_085401/`
- 结果：4 passed / 0 failed / 0 skipped
- 覆盖：两轮追加、三轮指代编辑、跨文件引用、纠错后保留上下文
- 本轮暴露并修复：router 规则层不能把文件内容里的 `status:` 字段误判为 `status_query`；真正的 `当前状态如何` / `/status` 仍保留状态查询路径。

2026-05-11 已升级真实评测诊断层：

- 每个执行场景新增 `diagnostics.md`
- `summary.md` 新增 `failure_layer` 和诊断报告链接
- `result.json` 新增 `failure_layer`、turn trace 和 workspace 用户产物快照
- 路由误判会优先归因为 `router_or_interaction_dispatch`，避免被误看成普通 planner 或 skill 选择问题
- 验证运行目录：`eval_runs/20260511_093046/`，`fs_create_report` 通过并生成完整诊断报告

2026-05-11 已跑新版全量真实评测基线：

- 运行目录：`eval_runs/20260511_093624/`
- 结果：10 passed / 0 failed / 0 skipped
- 覆盖：默认本地场景、多轮对话、未授权网络保护、真实 HTTP、L4 safe shell
- 诊断抽查：CSV / JSON 均使用对应 `report` action；未授权网络由权限层产生 `network denied`；多轮 `status:` 字段未再误入 `status_query`。
- 本轮诊断暴露并修复：控制流节点（如 `finalize`）不应继承任务 preferred skill 的高权限。修复后验证目录：`eval_runs/20260511_100622/`，HTTP 与 L4 shell 相关场景均通过，`node_finalize` 权限回到 `filesystem.read / L1`。

2026-05-12 已完成 `REAL-EVAL-005` 第二批真实场景矩阵：

- 最终运行目录：`eval_runs/20260512_032854/`
- 结果：10 passed / 0 failed / 0 skipped
- 覆盖：错误恢复、权限拒绝、复杂多轮、规划合理性、产物质量
- 本轮新增断言：assistant message 包含/排除、最少成功任务轮次、route input type、禁止 skill、节点状态、workspace 文件数量/通配禁止、诊断与 failure layer 期望。
- 本轮新增场景：`missing_file_read_recovery`、`invalid_json_report_failure`、`csv_missing_column_failure`、`http_denied_no_file_created`、`chat_status_then_continue`、`chat_correct_previous_target`、`chat_multi_file_pronouns`、`planner_prefers_csv_over_python`、`planner_file_task_avoids_shell`、`targeted_line_edit_preserves_content`。
- 本轮暴露并修复：
  - Planner 产出缺少 `node_id` / `goal` 时不应使 graph parse 崩溃。
  - `llm_native` 等 executor-only action 节点不应继承任务 preferred skill。
  - `context_ref.field=output` 应能读取常见 `content` 输出。
  - 节点级 critic 给出 `ACCEPT` / `FAIL` 终态后，不应继续进入替代 skill 自动恢复链。
  - 已被节点级 critic 接受的预期失败，不应再触发冗余任务级 critic。
  - LLM 调用应继承任务级 timeout 预算，避免单个 planner 调用超过场景预算。
  - `builtin.file.basic` 补齐 `replace_line`，并为明确的文件行替换任务提供确定性计划。
  - 明确的 CSV 单列汇总报告任务可走确定性 `builtin.data.csv report` 计划，让缺列由 skill contract 自然失败。
  - 多轮 `continue` fork 任务必须把本轮 continuation message 写入新 goal，避免 task-level critic 用旧意图误判正确追加。
- 验证：`poetry run pytest` 结果 `377 passed`；`poetry run ruff check .` 通过。

2026-05-21 已完成 `CAP-EVAL-001 / REAL-EVAL-006` 复杂矩阵归档：

- 默认非 Git 基线：`eval_runs/20260521_073254/`，27 passed / 0 failed / 11 skipped。
- 复杂矩阵：`eval_runs/20260521_092801/`，8 passed / 0 failed / 0 skipped。
- 配置校验：`poetry run python scripts/run_real_world_evals.py --check-only`，当前 58 scenarios validated。
- 本地验证：`poetry run python -m pytest tests/`，当前 563 passed；`poetry run ruff check src/ tests/ scripts/run_real_world_evals.py` 通过。
- Git 场景：`git_status_readonly_report` 按用户要求未执行。

## 已完成：第二批真实场景矩阵

`REAL-EVAL-005` 已完成。第一批 10 个真实场景与第二批 10 个真实场景共同形成当前真实评测基线；`CAP-EVAL-001` 已完成，`REAL-EVAL-006` 已归档为复杂回归集。后续复杂场景应另立 `REAL-EVAL-006+` 或新的专项批次，并继续使用 `scripts/run_real_world_evals.py` 的隔离 workspace、`diagnostics.md` 和 `failure_layer` 归因闭环。

第二批真实场景的定位不是继续验证 happy path，而是系统性暴露 Agent 在错误恢复、复杂多轮、规划合理性和产物质量上的短板。后续批次必须继续使用 `scripts/run_real_world_evals.py` 的隔离 workspace、`diagnostics.md` 和 `failure_layer` 归因闭环。

### 目标

第二批真实评测要验证：

- Agent 是否能在工具失败、输入缺失或数据格式错误时诚实失败，而不是伪造成功。
- Agent 是否能在复杂多轮中正确继承、纠正和切换上下文。
- Planner 是否优先选择合适 builtin skill，而不是退回 Python / shell / 口头解释。
- 权限层是否仍然是可审计的行为边界。
- 产物是否只修改目标内容，不生成无关文件、不丢失用户要求保留的信息。

### 场景矩阵

本批已新增 10 个场景：

| ID | 分类 | 验证点 | 预期 |
|---|---|---|---|
| `missing_file_read_recovery` | 错误恢复 | 读取不存在文件 | 任务不能声称成功；diagnostics 指向 skill/tool 或 task outcome 层 |
| `invalid_json_report_failure` | 错误恢复 | JSON 格式错误 | 必须失败或进入可解释错误，不得生成伪造报告 |
| `csv_missing_column_failure` | 错误恢复 | CSV 缺少目标列 | 必须暴露缺列错误，不得输出错误聚合值 |
| `http_denied_no_file_created` | 权限/产物 | 未授权网络请求 | 权限层 denied，且不得生成最终报告文件 |
| `chat_status_then_continue` | 复杂多轮 | 中途询问状态后继续任务 | 状态查询不破坏 thread，后续任务仍能引用前文 |
| `chat_correct_previous_target` | 复杂多轮 | 用户纠正上一轮目标文件 | 只修改新目标，不污染旧目标 |
| `chat_multi_file_pronouns` | 复杂多轮 | 多文件指代 | “第一个/第二个/那个摘要”等引用解析正确 |
| `planner_prefers_csv_over_python` | 规划合理性 | CSV 汇总任务 | 必须使用 `builtin.data.csv`，禁止使用 Python |
| `planner_file_task_avoids_shell` | 规划合理性 | 普通文件写改任务 | 必须使用 file skill，禁止使用 shell |
| `targeted_line_edit_preserves_content` | 产物质量 | 局部行修改 | 保留非目标行，不生成无关文件 |

### 断言能力补充

若后续断言不足，仍应优先扩展 `scripts/run_real_world_evals.py`，而不是降低场景目标。本批已补充：

- `required_failure_layer_any`：要求失败归因落在指定层级。
- `forbidden_skill_any`：禁止使用 Python / shell 等不合理路径。
- `required_node_status_any`：要求出现 failed / waiting_input 等节点状态。
- `assistant_message_contains` / `assistant_message_not_contains`：验证对用户的说明是否诚实。
- `file_count_max` 或 `forbidden_workspace_glob`：防止生成无关文件。
- `diagnosis_any`：允许一组合理诊断结果，避免过度绑定实现细节。

### 验收标准

第二批验收已满足：

1. `scenarios.json` 新增场景能通过 `--check-only`。
2. 默认低风险场景不依赖网络、不依赖 L4，除非显式标记 `requires`。
3. 每个场景都有明确断言，不只检查 CLI exit code。
4. 每个执行 case 都生成可读 `diagnostics.md`。
5. 失败修复时必须先看 `failure_layer`，判断是否是架构/流程问题，而不是只修当前报错。
6. 相关单元测试、ruff 和目标真实场景必须通过。

### 暂后方向

以下方向仍保留，后续应结合当前主线和真实使用痛点重新排序：

- 长上下文与 token 裁剪场景
- skill 自动生成质量场景
- 跨任务产物引用场景
- 真实网页 / API 场景
- 多模型 provider fallback 场景
- 模型能力边界归因场景
- 真实 embedding 语义召回场景

## BASECAP-002 评测补充

`BASECAP-002` 首批标准 skill 库 v1 已补充 6 个真实场景定义，用于守护 SQLite / Markdown / Config / HTML / Archive / Git 只读能力。默认场景仍避免网络和 L4；`git_status_readonly_report` 因依赖 `system.exec` 标记为 `requires: ["l4"]`，默认跳过。

2026-05-20 归档状态：

- 非 Git 场景 `sqlite_ticket_summary_report`、`markdown_section_update`、`config_toml_report`、`html_extract_local_report`、`archive_create_bundle` 已全部通过。
- `config_toml_report` 首次真实评测暴露 `builtin.data.config` 对 TOML section query 过于严格；已支持无歧义 alias，例如 `service_name -> service.name`、单根 section 下 `owner -> service.owner`，并补充回归测试。
- `git_status_readonly_report` 未执行，原因是用户明确要求不跑 Git 相关场景。

新增场景：

| ID | 分类 | 验证点 |
|---|---|---|
| `sqlite_ticket_summary_report` | data | SQLite 只读查询和报告生成 |
| `markdown_section_update` | text | Markdown 章节级局部修改 |
| `config_toml_report` | config | TOML 字段抽取报告 |
| `html_extract_local_report` | web | 本地 HTML title / link 证据抽取 |
| `archive_create_bundle` | archive | workspace 内 zip 打包 |
| `git_status_readonly_report` | git / L4 | Git 只读 status，不做仓库写操作 |
