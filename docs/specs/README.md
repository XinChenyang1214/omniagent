# 当前规格索引

Last reviewed: 2026-07-31

## 文档状态

- Document type: specification index
- Document status: active
- Implementation status: not_applicable
- Authority scope: 当前已实现行为的规格入口与边界

本目录描述 OmniAgent 当前实现必须保持的行为。代码和可重复验证是实现事实来源；
规格用于约束行为、数据契约、升级规则与安全边界。未来设计不应写入这里，除非已经
成为明确的兼容目标。

## 阅读顺序

1. [`current-system.md`](current-system.md)：当前系统边界、运行链路、接口、证据和安全不变量。
2. [`task-intake.md`](task-intake.md)：执行前任务类型、风险、工具、验证和确认预判。
3. [`agent-state.md`](agent-state.md)：Agent profile、用户偏好、项目状态和当前目标。
4. [`experience-record.md`](experience-record.md)：任务经验记录和 learning gate。
5. [`procedure-memory.md`](procedure-memory.md)：procedure、verified procedure 和 habit 生命周期。
6. [`skill-promotion.md`](skill-promotion.md)：habit 到 skill 的升级、lineage、contract 和权限。
7. [`learning-feedback.md`](learning-feedback.md)：任务结束时的学习反馈。
8. [`learning-correction.md`](learning-correction.md)：复用失败后的降级、冻结、复审和恢复。

## 其他有效契约

- [`../../CONFIGURATION.md`](../../CONFIGURATION.md)：配置文件和环境变量。
- [`../runbooks/real-world-evaluation.md`](../runbooks/real-world-evaluation.md)：真实评测执行流程。
- `src/omniagent/builtin_skills/*/README.md`：单个 builtin skill 的 action 与权限契约。
- `src/omniagent/prompts/*.md`：LLM prompt 输入输出约束。
- `memory/*/README.md`、`sandbox/README.md`、`skills/*/README.md`：模块边界。

## 权威规则

- 当前行为冲突：以代码、测试和可复现运行结果为事实，并修正规格漂移。
- 产品方向冲突：以 [`../product-architecture.md`](../product-architecture.md) 和
  [`../../ROADMAP.md`](../../ROADMAP.md) 为准。
- 历史专项、旧架构和旧评测基线只提供背景，不覆盖当前规格。
- 学习升级必须先有验证证据；fast path 不得绕过 contract、权限或 side-effect gate。

## 更新条件

以下变化必须同步相应规格：

- 运行链路、workspace artifact 或公开 CLI 行为改变；
- Agent state、experience、procedure、habit、skill schema 改变；
- promotion、validation、approval、correction 或恢复 gate 改变；
- 权限、沙箱、默认联网行为或外部副作用改变。
