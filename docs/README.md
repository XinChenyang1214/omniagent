# 文档治理入口

本目录是 OmniAgent 的文档状态与权威边界入口。它不替代已有顶层文档，而是说明这些文档分别处在什么生命周期、能约束什么工作、什么时候需要更新或归档。

## 当前诊断

- Workstream：文档治理
- Document type：当前状态索引 / 运维入口
- Document status：active
- Implementation status：not_applicable
- Next step：文档状态集中维护在 `docs/doc-registry.md`；当前开发主线已切换为 `META-010`，旧阶段开发文档已归档到 `archive/pre-product-redesign/`。

## 阅读顺序

理解当前项目时，优先按这个顺序看：

1. `README.md`：公开入口、快速开始和当前状态摘要。
2. `docs/doc-registry.md`：文档类型、生命周期状态、权威边界和替代关系。
3. `docs/product-architecture.md`：目标产品架构、学习闭环和后续产品路线取舍。
4. 当前活跃专项文档：`workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md`，对应 `META-010`。
5. `docs/specs/agent-state.md`、`docs/specs/experience-record.md`、`docs/specs/procedure-memory.md`、`docs/specs/skill-promotion.md`：`META-010` Phase 1-5 的实现规格。
6. `ARCHITECTURE.md`：旧实现架构和迁移输入，不再作为目标产品架构。
7. `ROADMAP.md` 与 `issue.md`：当前优先级、开放问题和已关闭记录。
8. `CONFIGURATION.md`、`REAL_WORLD_EVAL_PLAN.md`、模块 `README.md` 与 builtin skill 文档：配置、评测、模块和能力契约。

代码仍是事实来源。文档与实现冲突时，以代码和可验证行为为准；行为变化后再同步更新结果文档与 registry。

## 文档类型

- 当前状态 / 入口：说明当前已有能力、安装方式、使用方式和状态摘要。
- 架构 / 规格：约束当前实现和后续变更。
- 路线图 / issue：记录当前主线、未关闭问题和风险。
- 专项过程文档：记录某个升级、评测、修复或迁移如何推进。
- 验证文档：记录测试、评测、diagnostics、审计或验收证据。
- 运维 / runbook：记录命令、workspace 结构、诊断流程和维护方式。
- 探索笔记：保留想法和不确定方向；在提升为 architecture、spec、roadmap 或 active workstream 前，不作为实现契约。

## 当前文档地图

- 当前入口：`README.md`
- 项目规则：`AGENTS.md`
- 目标产品架构：`product-architecture.md`
- 当前活跃专项：`workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md`
- 当前实现规格：`specs/agent-state.md`、`specs/experience-record.md`、`specs/procedure-memory.md`、`specs/skill-promotion.md`
- 旧实现架构 / 迁移输入：`ARCHITECTURE.md`
- 配置：`CONFIGURATION.md`
- 当前优先级：`ROADMAP.md`、`issue.md`
- 归档旧开发文档：`archive/pre-product-redesign/`
- 评测运维：`REAL_WORLD_EVAL_PLAN.md`、`scripts/README.md`
- 模块文档：`memory/*/README.md`、`sandbox/README.md`、`skills/*/README.md`、`src/omniagent/builtin_skills/*/README.md`
- Prompt 模板：`src/omniagent/prompts/*.md`
- 早期概念笔记：`archive/pre-product-redesign/IDEAS.md`

## 修改规则

后续整理或新增文档时：

1. 先判断文档类型和生命周期状态，再决定是否编辑。
2. 文档权威、状态或替代关系变化时，同步更新 `docs/doc-registry.md`。
3. 不把 draft / exploration 笔记当成实现契约。
4. 专项完成并验证后，把长期有效行为沉淀到 architecture、spec、configuration、module 或 runbook 文档。
5. 历史过程文档默认保留；如需归档，必须有明确替代链接。

## 后续迁移建议

当前已经归档旧阶段开发文档。如果后续确认做更大的文档重组，可以继续采用：

```text
docs/
  README.md
  doc-registry.md
  workstreams/
    active/
    completed/
  archive/
    pre-product-redesign/
  validations/
  runbooks/
```

已完成迁移包括：把旧阶段开发文档移入 `docs/archive/pre-product-redesign/`，把 `META-010` 新开发契约放入 `docs/workstreams/active/`。后续若继续拆分 validations、runbooks 或 specs，应作为单独任务执行。
