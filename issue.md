# Issue Tracker

记录当前未关闭问题与近期已关闭问题。

> **修复规则**：碰到新 bug 时，**先问"这反映了哪个 META"**。如果是新的 META，先立 META 项再考虑补丁；如果命中已有 META，补丁直接进 git log，commit message 明确写 "根治待 META-XXX"。

---

## 当前优先级状态（2026-05-21）

**`CAP-EVAL-001` 复杂能力评测前置升级已完成。**

任务文档：[`COMPLEX_CAPABILITY_EVAL_UPGRADE.md`](./COMPLEX_CAPABILITY_EVAL_UPGRADE.md)

当前规则：

- `REAL-EVAL-006` 复杂真实场景矩阵已归档为回归集
- P1 / P2 / P3 暂停冻结解除，后续应按真实使用痛点重新排序
- 当前正在推进 `META-009` Runtime 分层执行与性能优化
- 已有 P2 项保留记录，等待重新排期
- `META-009` 第一轮已落地，后续继续补 validator 和条件化验证
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

## 📊 全局索引（2026-05-21 更新）

> **当前状态**：BASECAP-001、REAL-EVAL-005 和 CAP-EVAL-001 均已完成；真实评测已具备 `diagnostics.md`、completion evaluation、embedding summary 和 failure attribution 证据。当前场景文件总数 39；默认非 Git 基线 `eval_runs/20260521_073254/` 为 27 passed / 0 failed / 11 skipped，8 个复杂场景 `eval_runs/20260521_092801/` 为 8 passed / 0 failed / 0 skipped；`git_status_readonly_report` 按用户要求未执行，不纳入本次 baseline 归档通过口径。META-009 已启动首轮落地：`ExecutionPolicy`、策略 artifact / diagnostics、Planner capability index / expanded manifests 已完成，validator 和条件化验证仍待。
> 历史修复记录见 git log。

### 🔴 META（设计/架构层）

| ID | 一句话 | 优先级 | 备注 |
|---|---|---|---|
| **META-002** | 路由本体把管线指令/查询/生命周期/闲聊塞同一个 enum，靠关键词列表分流 | P2 暂停 | `CAP-EVAL-001` 完成后重新排序 |
| **META-005** | 失败不会反向传播到 router/dispatcher/planner，同一个坑会反复掉 | P2 暂停 | `CAP-EVAL-001` 完成后重新排序 |
| **META-009** | Runtime 缺少按任务确定性分层的执行策略，低风险结构化任务和 skill manifest 展开仍未完全按需 | 🟡 进行中 | 第一轮已落地 ExecutionPolicy 与 Planner manifest 数据分层；见 [`RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md`](./RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md) |

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

**完成记录**：见 [`BASIC_CAPABILITIES_UPGRADE.md`](./BASIC_CAPABILITIES_UPGRADE.md)。该项已关闭，不再作为当前阻塞项。

---

## 🔴 CAP-EVAL-001：复杂能力评测前置升级

**症结**：继续扩充复杂真实场景前，当前评测体系无法稳定区分模型能力不足、Agent 流程问题、工具/skill 问题、记忆召回问题和测试预期问题。任务级 completion evaluation、真实 embedding 与 diagnostics 归因证据已落地，下一步用复杂场景验证这些能力。

**目标**：

1. 新增 completion evaluation artifact，明确判断任务是否真正满足用户原始目标。
2. 在 diagnostics 中增加可审计的失败归因证据。
3. 引入 `model_capability` 归因，避免把模型能力边界误修成 Agent patch。
4. 接入 OpenAI-compatible 与进程内 local_model 真实 embedding，并保留 hash fallback。（已完成）
5. 完成后再启动 `REAL-EVAL-006` 复杂真实场景矩阵。

**状态**：✅ 完成。阶段 0 / 阶段 1 / 阶段 2 / 阶段 3 / 阶段 4 已归档，任务文档见 [`COMPLEX_CAPABILITY_EVAL_UPGRADE.md`](./COMPLEX_CAPABILITY_EVAL_UPGRADE.md)。

**当前验证**：最终 `poetry run python -m pytest tests/` 为 463 passed，`poetry run ruff check src/ tests/ scripts/run_real_world_evals.py` 通过，`python3 scripts/run_real_world_evals.py --check-only` 校验 39 个场景通过。

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

## 🟠 META-009：Runtime 分层执行与性能优化

**症结**：Runtime 目前默认把任务统一送入 `Planner -> Execution -> Critic -> Completion`。真实场景 `context_recall_hygiene_filters_failed_and_duplicate_memory` 显示，一个低风险结构化 file write 任务耗时 `104.448s`，其中 Planner / Critic / Completion 三次 LLM 调用合计 `28,906` tokens；Planner request payload 约 `29.9KB`，其中 `skill_registry_entries` 约 `26.7KB`。

**目标**：建立按任务确定性、风险和输入结构分层的执行策略。低风险、结构化、payload 完整的任务可走 fast path；自然语言、多步、指代、网络、L4、删除、失败恢复等场景仍走完整 Planner 和验证链路。

**修复方向**：

1. 新增 `ExecutionPolicy` / `PlanningPolicy`，在 Planner 调用前判断 `direct_execute` / `plan_required` / `plan_with_compact_registry`。
2. 第一阶段只允许显式 `executor + payload + L2 以内 + 无上下文指代` 的任务跳过 Planner。
3. Planner 仍需参与时，默认只发送 `SkillCapabilityIndex`；完整 skill manifest / contract 仅在显式指定、action 精确匹配、contract gap 或 replan 时按需展开。
4. 后续再条件化 task-level Critic / Completion，并以本地 artifact validator 替代部分低风险成功路径验证。
5. 每次策略决策必须写入 artifact 和 diagnostics，包含跳过原因、上下文字段大小和回退路径。

**边界**：不全局关闭 Planner，不削弱 permission / sandbox，不跳过高风险或复杂任务验证，不为了速度降低评测断言。

**专项文档**：[`RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md`](./RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md)。

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
