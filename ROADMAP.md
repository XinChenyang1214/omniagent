# OmniAgent 路线图

Last reviewed: 2026-06-22

## 文档状态

- Document type: roadmap
- Workstream: project planning
- Document status: active
- Implementation status: implementing
- Authority scope: 当前开发优先级、阶段顺序和迁移边界

当前路线图只描述从最新产品架构出发的开发主线。旧阶段开发细节已经归档到 [`docs/archive/pre-product-redesign/`](./docs/archive/pre-product-redesign/)，不再在本文件中展开。

## 当前结论

当前主线是 [`META-010` Agent usability and learning loop MVP](./docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md)。

项目从 2026-06-11 起按 [`docs/product-architecture.md`](./docs/product-architecture.md) 重新开发为以学习循环为中心的通用个人 Agent OS。现有 task-centric runtime 不删除，但下沉为新架构里的 Execution Runtime。

本项目目标是通用能力 Agent，不是代码助手、开发工具或单一场景自动化框架。后续路线应优先验证跨任务类型的目标理解、风险判断、工具选择、执行验证、记忆复用和学习纠错能力；开发维护任务只是早期高频落地场景之一。

当前开发判断：

- `docs/product-architecture.md` 是目标产品架构。
- `docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md` 是当前实现契约和验收矩阵。
- `ARCHITECTURE.md` 是旧实现 current-state inventory 和迁移输入，不再作为目标架构。
- `META-009`、`BASECAP-*`、`CAP-EVAL-*` 等旧开发文档只作历史和迁移参考。

## Current Roadmap

| Phase | Goal | Status | Exit evidence |
|---|---|---|---|
| Phase 0 | 文档权威切换与迁移基线 | completed | Registry、README、ROADMAP、AGENTS 指向 `META-010`；旧文档归档；Phase 1-5 specs 已补齐 |
| Phase 1 | Agent state | implemented | `AgentProfile`、`UserPreference`、`ProjectState`、`CurrentGoal` 可持久化、读取、更新摘要并在 CLI / REPL 中查看；任务 context / policy artifact 写入 state summary |
| Phase 2 | Experience records | implemented | 每次任务结束写 `ExperienceRecord`，success / failure / permission denied 有 evidence；record 引用 artifacts 和 validation，不复制大内容 |
| Phase 3 | Procedure memory | implemented | 可从 experience 提炼 `ProcedureDraft`；相似任务在 Planner 前写 match decision artifact；CLI 可 list / show / edit / review / disable / explain-match |
| Phase 4 | Habit fast path | implemented | verified procedure 满足 3 次成功、无近期失败后可升级 habit；低风险 habit 可成为 policy fast path candidate；artifact 记录 habit id、skipped stages、validator evidence 和 policy gate |
| Phase 5 | Skill promotion | implemented | `skill promote-habit` 可从稳定 habit 生成、验证并激活低风险 skill；skill 带 contract、权限边界、测试和 experience -> procedure -> habit -> skill lineage |
| Phase 6 | Learning feedback | verified | 任务结束回复展示 experience、procedure / habit 命中、复用判断、review / edit / disable 入口和下一次复用预期；`learning_loop_feedback_sequence` 验证连续任务中的生成、复用、解释、修正和治理，`learning_feedbacks` 已接入 evidence index / dashboard |
| Phase 7 | Learning correction | verified | matched procedure / habit 复用失败后写 `learning/correction.json`，旧学习对象进入 `correction_required`、降级或冻结，explain-match 解释不再复用，fresh evidence approval 后恢复；`learning_loop_correction_recovery` 验证闭环，`learning_corrections` 已接入 evidence index / dashboard |

## Immediate Next Work

当前 roadmap 的 Phase 0-7 MVP 主线已实现；跨 eval evidence index 已具备查询、报告视图和 dashboard/runbook 模板。Learning feedback 已让任务结束回复直接说明“这次学到了什么、是否会复用、还需要我做什么”，Learning correction 已让复用失败后的降级、冻结、复审和恢复可被解释与回归验证。下一步进入通用 Agent 初步可用版：先用多类真实个人任务验证学习闭环是否可感知、可复用、可纠错，再继续扩展具体 skill 类型和治理入口。首批 `TaskIntakeAssessment` 和 `general_agent_usability_*` 评测矩阵已加入并通过 `--check-only`，包含 5 类任务学习序列、5 个自然语言 tool-choice 场景和 5 个权限边界场景；离线 `OMNIAGENT_LLM_PROVIDER=test` 执行基线已跑通，下一步需要建立真实 LLM provider 基线并修复暴露的问题。

1. 运行并收敛通用任务初步可用评测集；当前首批覆盖文件整理、信息归纳、文档生成、数据处理和网页信息获取 5 类任务，包含成功、失败、procedure 复用、correction 断言、自然语言 tool-choice 和独立权限边界场景；离线 test provider 已通过，仍需真实 LLM provider 验证。
2. 按真实稳定 habit 继续扩展更多 skill promotion 类型；file（含 replace_line / copy）、JSON / JSONL data（含 jsonl / ndjson alias 和 report_lines）、CSV / TSV data、SQLite data、config（含 YAML / TOML / XML / dotenv / INI / properties alias）、plain text、markdown、HTML、archive（含 zip / tar / gztar / tgz / tar.gz alias）和 datetime 已作为低风险自动激活类型支持；active 低风险 `generated.habit.*` 显式 payload 已可在 contract / permission / side-effect gate 下 direct execute；read-only Git 和 safe shell 已作为 process-backed 手动审批类型支持，Python 已作为 code-execution-backed 手动审批类型支持，HTTP 和 browser fetch 已作为 network-backed 手动审批类型支持，并保持高风险审批 gate。
3. 继续扩展学习治理到更多对象级别的操作入口；统一只读审计、对象详情、历史事件和匹配解释视图已覆盖 experience / procedure / habit / skill 与 procedure / habit retrieval，统一 edit / promote / validate / decay / request-changes 入口已覆盖 procedure / habit，统一 validate 入口已覆盖 skill，统一 rollback 入口已覆盖 skill，统一 approve / reject / disable / enable 入口已覆盖 procedure / habit / skill。
4. 将 evidence dashboard 接入更多回归消费入口；当前已能生成失败修复入口、regression commands（含独立命令文件导出、failed scenario rerun 命令、eval coverage status / scenario / category / requires / skip reason 动态查询、used skill 动态查询、procedure id / habit id 动态查询、lineage skill 动态查询、promotion generation type / source target 动态查询、permission summary 动态查询、completion layer 非 `none` 动态查询、policy decision / skipped stage / policy risk flag 动态查询、policy reason 动态查询、policy context budget / request payload stats 高水位动态查询、node status `status:*` / `attempts:*` / `error:*` 动态查询、critic action `action:*` 动态查询、memory retrieval 低基数状态动态查询、LLM usage 低基数 token / duration / call-state 动态查询、learning kind 动态查询、learning validation 动态查询、skill validation 动态查询、learning review decision 动态查询和 learning audit event 动态查询）、eval coverage、skipped eval、skill/tool failure、model capability、node execution、critic action、completion evaluation、memory retrieval、LLM usage、runtime cost hotspot、execution policy、Planner skipped、policy risk flag、policy reason、policy context budget、request payload stats、permission governance、habit fast path、learning match explanation、learning gate、learning feedback、learning governance、learning history、learning approval、learning promotion、learning validation、learning edit、learning request-changes、learning decay、learning disable、learning restore、learning rollback、learning rejection、learning audit gap、skill lifecycle governance、generated habit skill usage、skill promotion coverage、skill approval gate 和 learning lineage cases。

Phase 1、Phase 2、Phase 3、Phase 4、Phase 5、Phase 6 和 Phase 7 已完成；learning-loop 真实评测 case、learning feedback task-ending reply、learning correction artifact / frozen reuse / fresh evidence restore、procedure edit / review 入口、统一 learning review / show / history / explain-match / edit / promote / validate / decay / request-changes / approve / reject / disable / enable / rollback 入口、experience review、habit review、skill validation / approval / rejection / disable / enable / rollback、跨 eval evidence index、evidence index scenario / category / requires / skip-reason / permission / completion-layer / policy-decision / skipped-stage / policy-risk-flag / policy-reason / policy-context-budget / request-payload-stats / node-status / critic-action / memory-retrieval / llm-usage / promotion-generation-type / promotion-source-target / learning-kind / learning-validation / skill-validation / review-decision / audit-event 查询视图、evidence dashboard regression commands（含 eval-coverage / scenario / used-skill / procedure-id / habit-id / lineage-skill / promotion-generation-type / promotion-source-target / permission-summary / policy-reason / policy-context-budget-hotspot / request-payload-stats-hotspot / learning-kind / learning-validation / skill-validation / review-decision / audit-event 动态查询）/ eval coverage / skipped eval / model capability / node execution / critic action / completion evaluation / memory retrieval / LLM usage / runtime cost hotspot / execution policy / Planner skipped / policy risk flag / policy reason / policy context budget / request payload stats / permission governance / habit fast path / learning match explanation / learning gate / learning feedback / learning correction / learning governance / learning history / learning approval / learning promotion / learning validation / learning edit / learning request-changes / learning decay / learning disable / learning restore / learning rollback / learning rejection / learning audit gap / skill lifecycle governance / skill promotion coverage / skill approval gate 视图、JSON / JSONL data habit promotion、active generated habit direct execute、CSV / TSV data habit promotion、SQLite data habit promotion、config / YAML / XML / dotenv / INI / properties habit promotion、plain text habit promotion、markdown habit promotion、HTML habit promotion、archive / tar.gz habit promotion、datetime habit promotion、read-only Git habit promotion（手动审批）、Python habit promotion（手动审批）、HTTP habit promotion（手动审批）、browser fetch habit promotion（手动审批）和 safe shell habit promotion（手动审批）已补齐。当前 spec：

- [`docs/specs/procedure-memory.md`](./docs/specs/procedure-memory.md)
- [`docs/specs/skill-promotion.md`](./docs/specs/skill-promotion.md)
- [`docs/specs/learning-feedback.md`](./docs/specs/learning-feedback.md)
- [`docs/specs/learning-correction.md`](./docs/specs/learning-correction.md)

## Migration Inputs

旧系统中仍可复用的部分如下，但它们不再决定产品目标：

| Legacy asset | New role | Treatment |
|---|---|---|
| `runtime.py`、`execution_engine.py`、`dispatcher.py` | Execution Runtime | 保留并下沉为执行层 |
| `execution_policy.py` | Decision-depth policy seed | 扩展为 procedure / habit fast path 的策略入口 |
| `memory.py`、`graph_memory.py`、`hybrid_memory.py`、`vector_recall.py` | Context OS seed | 从 recall 系统升级为 typed memory |
| `workflow_compiler.py` | Procedure extraction seed | 改造为 experience -> procedure draft 的提炼器 |
| `skill_lifecycle.py`、`skill_auto_generation.py` | Skill promotion seed | 只在 habit 稳定后进入 skill promotion |
| `workspace/tasks/`、`eval_runs/`、diagnostics | Evidence Store seed | 统一 evidence schema，用于学习升级和回归验证 |
| builtin skills | Capability inventory | 可复用，但不再把“增加更多 builtin skill”当成智能主线 |

## Current Non-Goals

这些方向当前不进入实现范围：

- Web UI / Desktop App / Mobile / messaging 多入口。
- 多用户、多租户、分布式执行。
- 删除并重写整个旧 runtime。
- 为了展示能力继续铺大量 builtin skill。
- 允许未验证学习结果自动改变高风险行为。
- 默认联网或引入远端服务。

## Validation Direction

后续验证要从旧 runtime 成本优化转向 learning-loop 证据：

- 单元测试覆盖 schema、promotion gate、permission gate。
- 集成测试覆盖 task -> experience -> procedure -> reuse。
- 真实场景评测新增 learning-loop case。
- 每个完成项必须能回到 `AGENT_USABILITY_LEARNING_LOOP_MVP.md` 的验收矩阵。
- 旧 `REAL-EVAL-006` 和 `META-009` 评测作为回归基线保留，不再是主验收目标。

## Archived History

旧阶段开发文档保留在 [`docs/archive/pre-product-redesign/`](./docs/archive/pre-product-redesign/)：

- `BASIC_CAPABILITIES_UPGRADE.md`
- `BASIC_CAPABILITIES_V1_UPGRADE.md`
- `COMPLEX_CAPABILITY_EVAL_UPGRADE.md`
- `RUNTIME_ORCHESTRATION_PERFORMANCE_UPGRADE.md`
- `IDEAS.md`

这些文档不再作为当前实现契约。需要查看旧实现为什么存在、哪些能力可迁移、旧评测基线是什么时，再读取这些归档文档。

## Decision Priority

1. 当前实现必须先对齐 [`docs/product-architecture.md`](./docs/product-architecture.md) 和 [`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](./docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md)。
2. 学习必须发生在验证之后；没有 evidence 的经验不能升级为 habit 或 skill。
3. 旧 runtime 能复用则复用，但不能让旧 task pipeline 继续定义产品形态。
4. 保持本地优先、单用户、可审计、轻量依赖。
5. 高风险行为继续受权限和用户确认约束。
