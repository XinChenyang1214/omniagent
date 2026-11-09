# 文档登记表

Last reviewed: 2026-07-31

本表只登记当前 governing 文档、长期操作文档和具有独立历史价值的文档组。
生成报告、eval run、临时分析和每个模块 README 不逐项登记。

## 状态语义

- `active`：当前有效。
- `completed`：工作已完成，保留为验收或迁移记录。
- `archived`：仅有历史权威，不约束当前实现。
- `deleted`：内容已完整沉淀且没有独立保留价值。

## 当前有效文档

| Path | Role | Authority scope | Implementation | Replacement |
|---|---|---|---|---|
| `README.md` | entry | 安装、快速开始、公开命令和状态摘要 | current | none |
| `AGENTS.md` | project rules | 工程规则、安全边界、验证和文档维护 | current | none |
| `docs/README.md` | governance entry | 阅读顺序、文档区域和维护规则 | current | none |
| `docs/doc-registry.md` | registry | 权威、生命周期与替代关系 | current | none |
| `docs/specs/README.md` | spec index | 当前规格入口 | current | none |
| `docs/specs/current-system.md` | system spec | 当前系统边界、链路、模块、证据和安全不变量 | verified_offline | root `ARCHITECTURE.md` |
| `docs/specs/task-intake.md` | behavior spec | Task Intake schema、时机和边界 | verified | none |
| `docs/specs/agent-state.md` | data spec | Agent state schema 和存储边界 | implemented | none |
| `docs/specs/experience-record.md` | data spec | ExperienceRecord 和 learning gate | implemented | none |
| `docs/specs/procedure-memory.md` | lifecycle spec | procedure / habit 生命周期与 fast path | implemented | none |
| `docs/specs/skill-promotion.md` | lifecycle spec | habit -> skill gate、lineage、权限和验证 | implemented | none |
| `docs/specs/learning-feedback.md` | output spec | 任务结束学习反馈 | verified | none |
| `docs/specs/learning-correction.md` | correction spec | 复用失败、冻结、复审和恢复 | verified | none |
| `docs/product-architecture.md` | product architecture | 产品目标和长期取舍 | partially_implemented | none |
| `CONFIGURATION.md` | config spec | 配置、环境变量和 provider | implemented_unverified | none |
| `ROADMAP.md` | roadmap | 当前优先级和阶段结果 | current | none |
| `issue.md` | issue tracker | 未关闭问题和验证阻塞 | current | historical issue archive |
| `docs/runbooks/real-world-evaluation.md` | runbook | 评测选择、执行、归因和证据消费 | verified_offline | root `REAL_WORLD_EVAL_PLAN.md` |
| `CLAUDE.md` | compatibility entry | 引导其他助手读取 `AGENTS.md` 和当前文档入口 | current | duplicated guidance removed |

## 分布式模块契约

以下路径在各自模块范围内有效：

- `scripts/README.md`
- `sandbox/README.md`
- `memory/*/README.md`
- `skills/*/README.md`
- `agents/main/skills/README.md`
- `shared/skills/README.md`
- `src/omniagent/builtin_skills/*/README.md`
- `src/omniagent/builtin_skills/*/tests/README.md`
- `src/omniagent/prompts/*.md`

## 完成记录

| Path | Status | Historical scope | Current replacement |
|---|---|---|---|
| `docs/workstreams/completed/AGENT_USABILITY_LEARNING_LOOP_MVP.md` | completed | META-010 Phase 0–7 实施与验收 | `docs/specs/` + `ROADMAP.md` |

当前没有 active workstream。下一项开发在目标、验收和范围明确后再创建；不复用已完成
META-010 文档承载新工作。

## 归档组

| Path | Status | Scope | Current replacement |
|---|---|---|---|
| `docs/archive/pre-product-redesign/` | archived | 学习闭环重构前的 task runtime、能力升级和旧评测专项 | `docs/specs/current-system.md`、产品架构和模块规格 |
| `docs/archive/project-history/ISSUE_TRACKER_THROUGH_2026-06.md` | archived | 旧 META / ISSUE 和评测演进 | `issue.md` |
| `docs/archive/project-history/REAL_WORLD_EVAL_HISTORY_THROUGH_2026-06.md` | archived | 旧评测计划、阶段基线和演进记录 | `docs/runbooks/real-world-evaluation.md` |

## 本次处置

- 旧根 `ARCHITECTURE.md` 移至
  `docs/archive/pre-product-redesign/ARCHITECTURE_TASK_RUNTIME.md`。
- META-010 从 `workstreams/active/` 移至 `workstreams/completed/`。
- 旧 issue 全量记录和真实评测历史分别归档，根 tracker 与 runbook 只保留当前信息。
- `docs/archive/pre-product-redesign/IDEAS.md` 已删除；其 learning-as-memory 概念已
  完整沉淀到产品架构、procedure memory 和 skill promotion 规格。

## 验证限制

`verified_offline` 只表示离线测试和评测通过。真实 LLM tool-choice 基线尚未完成，
不得把离线 evidence 扩大解释为真实模型验证。
