# Scripts

## `run_real_world_evals.py`

运行真实场景自主评测。脚本会为每个场景创建隔离 workspace、独立 SQLite 和独立日志，执行 OmniAgent CLI，收集任务产物、LLM 调用、节点状态和权限记录，并输出失败归因。

每个已执行场景都会生成：

- `cases/<scenario_id>/diagnostics.md`：首选排查入口，包含 turn trace、task intake、skill、权限、节点、critic、LLM 调用和 workspace 产物快照
- `cases/<scenario_id>/result.json`：结构化结果，包含 `diagnosis`、`failure_layer` 和原始 evidence
- `turn_XX_*`：每轮 CLI 命令、stdout、stderr 和 parsed JSON

常用命令：

```bash
python3 scripts/run_real_world_evals.py --list
python3 scripts/run_real_world_evals.py --check-only
python3 scripts/run_real_world_evals.py
python3 scripts/run_real_world_evals.py --scenario csv_sales_summary
OMNIAGENT_LLM_PROVIDER=test python3 scripts/run_real_world_evals.py --scenario general_agent_usability_file_organization
OMNIAGENT_LLM_PROVIDER=test python3 scripts/run_real_world_evals.py --scenario-category general_agent_usability --scenario-glob "general_agent_usability_*_tool_choice"
python3 scripts/run_real_world_evals.py --scenario-category general_agent_usability --scenario-glob "general_agent_usability_*_tool_choice" --dry-run
python3 scripts/run_real_world_evals.py --include-network
python3 scripts/run_real_world_evals.py --include-l4
```

`--scenario-category` 可按 category 选择场景，`--scenario-glob` 可按场景 id glob 选择子集；两者同时使用时必须同时匹配。`--dry-run` 只写 selected / skipped 预览 summary，不执行任务。`OMNIAGENT_LLM_PROVIDER=test` 可用于离线基线和回归调试，不调用外部 LLM；真实模型能力仍需使用实际 provider 单独验证。

场景定义见 [`../evals/real_world/scenarios.json`](../evals/real_world/scenarios.json)，流程说明见 [`../REAL_WORLD_EVAL_PLAN.md`](../REAL_WORLD_EVAL_PLAN.md)。
