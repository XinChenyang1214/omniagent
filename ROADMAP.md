# OmniAgent 路线图

Last reviewed: 2026-07-31

## 当前结论

META-010 学习闭环 MVP 已完成并移入
[`docs/workstreams/completed/`](docs/workstreams/completed/AGENT_USABILITY_LEARNING_LOOP_MVP.md)。
当前没有 active workstream；下一阶段目标是用真实通用任务证明现有闭环可用，而不是
继续横向堆 builtin skill。

当前行为以 [`docs/specs/`](docs/specs/README.md) 为规格，长期产品方向以
[`docs/product-architecture.md`](docs/product-architecture.md) 为准。

## 已完成基线

| Phase | 结果 | 状态 |
|---|---|---|
| 0 | 文档权威切换与迁移基线 | completed |
| 1 | Agent state | implemented |
| 2 | Experience records | implemented |
| 3 | Procedure memory | implemented |
| 4 | Habit fast path | implemented |
| 5 | Skill promotion | implemented |
| 6 | Learning feedback | verified |
| 7 | Learning correction | verified |

系统已经具备 Task Intake、experience → procedure → habit → skill、用户治理、
失败纠正、evidence index 和 dashboard。详细行为见
[`docs/specs/current-system.md`](docs/specs/current-system.md)。

## 当前产品验证目标

### 1. 建立真实 LLM 通用任务基线

范围是文件整理、信息归纳、文档生成、数据处理和网页信息五类任务，覆盖：

- 任务类型、风险、工具、验证和确认预判；
- 成功路径与失败路径；
- procedure / habit / skill 复用；
- 权限边界；
- learning feedback 与 correction；
- 自然语言 tool choice。

当前证据：

- 58 个场景定义通过 `--check-only`；
- 离线通用 Agent 基线 15 passed / 0 failed / 0 skipped；
- 真实 provider dry-run 已正确选中五个 tool-choice 场景；
- OpenCode Zen `deepseek-v4-flash-free` provider 和 free-only guard 已通过离线契约测试；
- 尚未使用有效 OpenCode Key 执行真实 health check。

下一验收点：仅通过环境变量注入有效 `OPENCODE_API_KEY`，建立真实 LLM 五场景
基线，并按 failure layer 修复暴露的问题。真实调用需要单独确认。

### 2. 用真实稳定 habit 驱动能力扩展

当前已支持 file、JSON/JSONL、CSV/TSV、SQLite、config、plain text、Markdown、
HTML、archive 和 datetime 的低风险 promotion；Git、Python、HTTP、browser 和
safe shell 保留人工审批 gate。

只有真实重复任务形成稳定 habit 后，才增加或扩展 promotion 类型。不得以新增
builtin skill 数量替代学习能力验证。

### 3. 收敛学习治理体验

现有统一入口已覆盖 experience、procedure、habit 和 skill 的查看、历史、解释、
编辑、升级、验证、审批、拒绝、禁用、恢复和 rollback。后续只针对真实治理摩擦
调整，不预先扩张命令面。

### 4. 控制 evidence 工具复杂度

Evidence index/dashboard 已覆盖运行状态、失败层级、policy、权限、LLM usage、
learning evidence 和 skill lifecycle。后续优先拆分职责、保持可测试性，避免
`run_real_world_evals.py` 继续成为新的单体模块。

## 当前非目标

- Web / Desktop / Mobile / messaging 多入口；
- 多用户、多租户和分布式执行；
- 删除并重写整个 Execution Runtime；
- 默认联网或引入远端状态服务；
- 未验证学习结果自动改变高风险行为；
- 没有真实使用证据时继续铺大量能力类型。

## 开放问题

只在 [`issue.md`](issue.md) 维护。已完成过程和旧基线位于
[`docs/archive/`](docs/archive/README.md)，不再混入当前路线图。
