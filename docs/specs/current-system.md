# OmniAgent 当前系统规格

Last reviewed: 2026-07-31

## 文档状态

- Document type: system specification
- Document status: active
- Implementation status: verified_offline
- Authority scope: 当前产品边界、运行链路、核心模块、持久化证据和安全不变量
- Supersedes: root `ARCHITECTURE.md`
- Verification limitation: 真实 LLM tool-choice 基线仍受本地 provider 凭据配置阻塞

## 1. 产品边界

OmniAgent 当前是单用户、单机、CLI 优先的本地个人 Agent Runtime。产品主轴是
“目标—执行—证据—学习—复用”，TaskGraph runtime 是执行层，不再单独定义产品。

当前系统必须：

- 处理文件、信息归纳、文档、数据、网页、规划、轻量自动化和开发维护等通用任务；
- 在执行前记录任务类型、风险、候选工具、验证方式和确认需求；
- 持久化 Agent 状态、任务证据和可治理的学习对象；
- 对重复任务优先评估 procedure / habit 复用；
- 只允许通过验证证据升级 procedure、habit 和 skill；
- 允许用户检查、编辑、禁用、审批、拒绝和恢复学习结果；
- 默认本地运行，只有 LLM 调用和显式授权的网络任务可以访问网络。

当前不包含 Web / Desktop / Mobile UI、多用户、多租户、分布式执行或默认联网服务。

## 2. 运行链路

```text
CLI / Chat
  -> InteractionRouter
  -> RuntimeGateway
  -> Agent state + TaskIntakeAssessment
  -> Procedure match + Habit candidate
  -> ExecutionPolicy
       -> direct_execute（仅满足低风险、contract、权限和副作用 gate）
       -> Planner -> TaskGraph
  -> ExecutionEngine
       -> SkillDispatcher -> builtin / generated skill / executor
       -> PermissionManager + Sandbox
       -> retry / degrade / replan / checkpoint
  -> Critic
  -> CompletionEvaluator
  -> ResponseGenerator
  -> ExperienceRecord
  -> Learning feedback / correction
  -> Procedure / habit / skill lifecycle
```

### 2.1 执行前

`EmbeddedRuntime` 必须在正式执行前：

1. 读取 bootstrap documents、Agent state 和相关记忆；
2. 生成并落盘 [`TaskIntakeAssessment`](task-intake.md)；
3. 解释 procedure match，并记录候选、置信度和拒绝原因；
4. 计算 habit fast-path candidate；
5. 由 `ExecutionPolicy` 决定 `direct_execute` 或 `plan_required`。

Task intake 是可审计启发式预判，不替代 Planner、权限判断或用户确认。

### 2.2 执行中

- Planner 输出 `TaskGraph` DAG；action、approval、decision、wait、replan 和 finalize
  节点由 `ExecutionEngine` 处理。
- `SkillDispatcher` 的选择顺序必须尊重显式 skill、registry contract、executor hint
  和受控自动生成边界。
- 节点输出可以作为 `dependency_outputs` 注入下游节点。
- retry、degrade、replan 和 checkpoint 必须留下事件与 artifact。
- direct execute 与 generated habit skill 仍必须通过 contract、permission、
  lifecycle status 和 side-effect gate。

### 2.3 执行后

- Critic 判断执行质量和恢复动作。
- CompletionEvaluator 独立判断是否满足用户目标并记录失败层级。
- 每次任务尝试都写入 ExperienceRecord，包括成功、失败、权限拒绝和阻塞路径。
- 任务结束回复附带学习反馈；发生复用失败时写 learning correction。
- 只有满足对应规格的 evidence gate，学习对象才能升级或恢复。

## 3. 核心模块边界

| 区域 | 主要模块 | 当前职责 |
|---|---|---|
| 入口与协调 | `cli.py`, `interaction.py`, `gateway.py` | CLI、对话路由、子系统装配和公开操作入口 |
| 任务预判 | `task_intake.py` | 确定性、可审计的任务类型/风险/工具/验证/确认预判 |
| 执行策略 | `execution_policy.py`, `planner.py` | 决策深度、fast path 和 TaskGraph 规划 |
| 执行与验证 | `runtime.py`, `execution_engine.py`, `dispatcher.py`, `critic.py`, `completion_evaluator.py` | DAG 执行、skill 选择、恢复和完成度验证 |
| 状态与上下文 | `agent_state.py`, `memory.py`, `graph_memory.py`, `hybrid_memory.py`, `vector_recall.py` | typed Agent state 与混合召回 |
| 学习闭环 | `experience.py`, `procedure_memory.py`, `learning_governance.py`, `skill_lifecycle.py` | experience、procedure、habit、skill 和用户治理 |
| 安全 | `permissions.py`, `sandbox.py` | L1–L5 权限、workspace/process/container 隔离 |
| 持久化与证据 | `storage.py`, `workspace.py`, `events.py` | SQLite 状态、任务 artifact 和事件流 |
| 模型接入 | `llm/`, `embeddings.py` | OpenAI-compatible LLM、OpenCode Zen / OpenRouter 免费模型与可选 embedding |

## 4. 当前数据与证据契约

每个任务目录按实际路径存在时至少可包含：

```text
workspace/tasks/<task_id>/
  plan/
    intake_assessment.json
    policy_decision.json
  procedure/
    procedure_match.json
    habit_fast_path.json
  nodes/
  critic/
  completion/
  llm_calls/
  experience/
  learning/
    feedback.json
    correction.json
```

相关对象的详细 schema 和 gate 由本目录的子规格定义。大产物不复制到 learning
record；使用 artifact path、evidence ref 和 lineage 引用。

跨评测证据由 `scripts/run_real_world_evals.py` 汇总到 `eval_runs/`。这些运行结果
是时间与环境相关的 evidence，不替代当前规格。

## 5. 公开操作面

当前 CLI 操作面包括：

- `run`、`task`、`chat`：执行、查看和恢复任务/对话；
- `state`：查看 Agent state；
- `procedure`：查看、解释、编辑、复审、升级和禁用 procedure；
- `learning`：统一治理 experience、procedure、habit 和 skill；
- `skill`、`workflow`：skill 生命周期和 legacy workflow candidate 入口；
- `scheduler`：timer / external event checkpoint；
- `llm`：健康检查与调用统计。

CLI 的详细命令以 `omniagent --help` 和 [`../../README.md`](../../README.md) 为准。

## 6. 安全与学习不变量

1. 默认不访问外部网络；网络能力需要显式配置和权限。
2. 高风险、删除、外部路径、进程执行和代码执行不能因 habit 或 fast path 绕过确认。
3. 未验证的单次成功不能直接成为 habit 或 active skill。
4. workflow candidate 不能绕过 habit gate 直接激活 skill。
5. 复用失败必须影响原学习对象的复用资格，并留下 correction evidence。
6. 用户禁用、拒绝或请求修改后，对象不得继续自动复用。
7. 敏感凭据只从环境变量注入，不写入配置、日志、测试或文档。
8. OpenRouter 默认仅允许 `openrouter/free` 或明确的 `:free` 模型，不隐式回退到
   付费模型；实际被路由的模型必须写入 LLM 响应和调用证据。
9. OpenCode Zen 默认仅允许以 `-free` 结尾的 Chat Completions 模型，不隐式回退到
   付费模型；免费模型不得用于个人信息、密钥、私有代码或其他机密内容。
10. OpenCode Zen 匿名模式默认关闭；显式开启后也只允许 `opencode` provider 在
    free-only 模式下向 `*-free` 模型发送无 Key 请求，不放宽其他 provider 或模型。

## 7. 当前验证状态

截至本次审查：

- `poetry run python -m pytest tests/ -q`：603 passed，31 subtests passed；
- `poetry run ruff check src/ tests/ scripts/run_real_world_evals.py`：通过；
- `scripts/run_real_world_evals.py --check-only`：58 个场景定义通过；
- 通用 Agent 离线基线：15 passed / 0 failed / 0 skipped；
- OpenCode Zen 匿名 health check：2026-07-31 使用明确清空所有 LLM Key 的环境
  真实运行通过；请求模型 `deepseek-v4-flash-free`，响应实际模型
  `deepseek-v4-flash`；
- 真实 provider tool-choice 五场景基线：尚未执行。

匿名 health check 只证明当前免费端点连通和结构化响应可用，不证明匿名能力长期
稳定，也不能据此宣称真实模型工具选择已经验证。

## 8. 规格关系

- 产品目标与长期取舍：[`../product-architecture.md`](../product-architecture.md)
- 当前规格索引：[`README.md`](README.md)
- 已完成 META-010 验收记录：
  [`../workstreams/completed/AGENT_USABILITY_LEARNING_LOOP_MVP.md`](../workstreams/completed/AGENT_USABILITY_LEARNING_LOOP_MVP.md)
- 后续优先级：[`../../ROADMAP.md`](../../ROADMAP.md)
- 当前问题：[`../../issue.md`](../../issue.md)
