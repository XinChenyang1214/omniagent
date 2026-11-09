# OmniAgent 文档入口

Last reviewed: 2026-07-31

本目录区分当前规格、产品方向、操作手册、完成记录和历史归档。文件位置不自动
赋予权威；具体状态和替代关系以 [`doc-registry.md`](doc-registry.md) 为准。

## 从哪里开始

想了解当前已经实现什么：

1. [`specs/README.md`](specs/README.md)
2. [`specs/current-system.md`](specs/current-system.md)
3. 对应的子规格

想了解产品要走向哪里：

1. [`product-architecture.md`](product-architecture.md)
2. [`../ROADMAP.md`](../ROADMAP.md)
3. [`../issue.md`](../issue.md)

想运行或诊断项目：

1. [`../README.md`](../README.md)
2. [`../CONFIGURATION.md`](../CONFIGURATION.md)
3. [`runbooks/real-world-evaluation.md`](runbooks/real-world-evaluation.md)

## 当前文档地图

```text
docs/
  README.md
  doc-registry.md
  product-architecture.md
  specs/                         # 当前有效行为契约
    README.md
    current-system.md
    task-intake.md
    agent-state.md
    experience-record.md
    procedure-memory.md
    skill-promotion.md
    learning-feedback.md
    learning-correction.md
  runbooks/                      # 当前可重复操作流程
    real-world-evaluation.md
  workstreams/
    completed/                   # 已完成专项的验收和迁移记录
  archive/                       # 不再约束当前实现的历史材料
```

## 文档角色

- `specs/`：当前行为、schema、gate 和安全不变量，governing。
- `product-architecture.md`：目标产品形态和长期取舍，governing for direction。
- `ROADMAP.md`：当前优先级和下一阶段结果，governing for planning。
- `issue.md`：仅保留未关闭问题。
- `runbooks/`：当前可重复执行的操作流程。
- `workstreams/completed/`：已完成工作及其验收证据，historical record。
- `archive/`：旧架构、旧过程和历史 tracker，historical only。
- 模块 README 与 prompt：各自目录范围内的契约。

## 维护规则

1. 当前行为变化时，先更新对应规格，不把长期事实只留在 workstream 或 issue。
2. 新专项只有在确实需要跨阶段协调时才创建 active workstream。
3. 专项完成后，把长期行为沉淀到规格，把操作沉淀到 runbook，再移动到 completed。
4. Issue 关闭后从当前 tracker 移除；历史由 Git 和必要归档承担。
5. 生成的 eval 结果属于 evidence，不进入 durable registry。
6. 归档前必须有当前替代文档；已被完整吸收且无独立历史价值的探索稿可以删除。
7. 文档与代码冲突时，以代码和可重复验证为事实，随后修正规格漂移。
