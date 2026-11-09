# 真实场景评测 Runbook

Last reviewed: 2026-07-31

## 文档状态

- Document type: validation runbook
- Document status: active
- Implementation status: verified_offline
- Authority scope: 真实评测的选择、执行、归因、证据消费和安全边界

## 目标

通过隔离 workspace、明确断言和分层 diagnostics 验证 Agent 是否真正完成任务，
并把失败归因到正确层级，而不是只检查 CLI exit code。

场景定义位于 `evals/real_world/scenarios.json`，runner 位于
`scripts/run_real_world_evals.py`。

## 安全边界

- 默认只运行离线、非 L4 场景。
- 网络、L4 shell、复杂场景必须使用相应显式 flag。
- 真实 LLM 调用会产生网络请求和 token 消耗，运行前必须确认 provider、model、
  api_base 和 API key 指向预期环境。
- 不把真实 key 写入配置、命令记录、文档或评测产物。
- 不使用真实生产数据、真实账号或不可逆外部写入。
- 删除、发布、外部系统写入或可能付费的动作仍需用户明确确认。

## 常用命令

```bash
# 只检查场景定义，不执行
poetry run python scripts/run_real_world_evals.py --check-only

# 列出场景
poetry run python scripts/run_real_world_evals.py --list

# 默认离线基线
OMNIAGENT_LLM_PROVIDER=test \
  poetry run python scripts/run_real_world_evals.py

# 单场景
poetry run python scripts/run_real_world_evals.py --scenario <scenario_id>

# 显式扩展
poetry run python scripts/run_real_world_evals.py --include-network
poetry run python scripts/run_real_world_evals.py --include-l4
poetry run python scripts/run_real_world_evals.py --include-complex
```

真实 provider 在正式执行前应先做 dry-run 选择检查和健康检查：

```bash
poetry run python scripts/run_real_world_evals.py \
  --scenario-glob 'general_agent_usability_*_tool_choice' \
  --expect-selected-count 5 \
  --require-real-llm \
  --dry-run

poetry run python scripts/run_real_world_evals.py \
  --scenario-glob 'general_agent_usability_*_tool_choice' \
  --expect-selected-count 5 \
  --require-real-llm \
  --real-llm-health-check
```

第二条命令会真实调用 LLM，必须先获得相应授权并确认凭据。

## 标准执行流程

1. 运行 `--check-only`，确认 schema 和场景引用有效。
2. 使用 scenario、glob、category 和 expected count 明确选择范围。
3. 对真实 provider 先 dry-run，确认不会误选场景。
4. 需要真实模型时运行 health check；配置失败应在场景执行前停止。
5. 执行选定场景。
6. 先读 `eval_runs/<run_id>/summary.md`。
7. 对失败 case 先读 `cases/<scenario_id>/diagnostics.md`。
8. 必要时再读 `result.json` 和对应 workspace task artifacts。
9. 按失败层级修复正确边界。
10. 重跑目标单测、目标场景、Ruff；影响面较大时跑全量测试。
11. 重建 evidence index 或 dashboard，并更新当前 issue / roadmap。

## 失败归因

优先区分：

- `router_or_interaction_dispatch`
- `planner_or_task_graph`
- `skill_selection`
- `permission_policy_or_scenario_config`
- `executor_or_skill_contract`
- `critic_or_recovery`
- `completion_evaluation`
- `response_generation`
- `model_capability`
- `infrastructure`

真实 provider 的认证、endpoint、网络或 health check 失败属于 infrastructure，不得
伪装为 tool-choice、Planner 或 skill 回归。模型已经获得充分上下文但仍无法满足
schema 或推理目标时，才考虑 `model_capability`。

## 重点证据

```text
eval_runs/<run_id>/
  summary.md
  summary.json
  cases/<scenario_id>/
    diagnostics.md
    result.json
  workspaces/<scenario_id>/tasks/<task_id>/
    plan/
    procedure/
    nodes/
    critic/
    completion/
    llm_calls/
    experience/
    learning/
```

运行后重点检查：

- TaskIntakeAssessment 与场景声明是否一致；
- `policy_decision.json` 是否正确选择 direct execute / plan required；
- 实际使用的 skill、权限和 sandbox；
- node status、attempt、error 和 Critic action；
- completion failure layer；
- experience、procedure match、habit fast path、learning feedback/correction；
- skill lineage、validation、approval 和 lifecycle。

## Evidence index 与目标审计

```bash
poetry run python scripts/run_real_world_evals.py --build-evidence-index
poetry run python scripts/run_real_world_evals.py --query-evidence-index
poetry run python scripts/run_real_world_evals.py --evidence-dashboard
poetry run python scripts/run_real_world_evals.py \
  --audit-general-agent-usability \
  --audit-allow-incomplete
```

Evidence index 是跨 run 查询入口，dashboard 是回归消费视图；两者都是生成证据，
不作为永久规格。目标审计必须同时区分：

- 场景定义覆盖；
- required passed evidence；
- learning governance evidence；
- 真实 LLM preflight；
- 真实 LLM tool-choice baseline；
- 最近的配置或基础设施阻塞。

## 当前基线

- 场景定义：58 个，通过 `--check-only`。
- 通用 Agent 离线基线：
  `eval_runs/general_agent_usability_offline_latest/20260623_022106/`，
  15 passed / 0 failed / 0 skipped。
- 真实 provider 最新尝试：
  `eval_runs/general_agent_usability_real/20260623_033433/`，
  health check 因 401 `invalid_api_key` 在场景执行前停止。

历史评测演进和旧 run 记录见
[`../archive/project-history/REAL_WORLD_EVAL_HISTORY_THROUGH_2026-06.md`](../archive/project-history/REAL_WORLD_EVAL_HISTORY_THROUGH_2026-06.md)。
