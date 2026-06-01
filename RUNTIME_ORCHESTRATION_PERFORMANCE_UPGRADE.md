# Runtime 分层执行与性能优化升级（META-009）

## 背景

`ISSUE-017` 修复后，Planner 上下文里的召回冗余已经显著下降，但真实场景 `context_recall_hygiene_filters_failed_and_duplicate_memory` 暴露了新的性能瓶颈：

- 总耗时：`104.448s`
- LLM 调用：3 次
- 总 tokens：`28,906`
- Planner：`20,505 tokens / 47.0s`
- Critic：`3,716 tokens / 24.5s`
- Completion evaluator：`4,685 tokens / 32.7s`
- Planner request payload：约 `29.9KB`
- 其中 `skill_registry_entries`：约 `26.7KB`

这说明当前慢点主要不在 executor，而在统一编排策略：即使用户已经给出结构化 `executor + payload`，Runtime 仍默认走完整 `Planner -> Execution -> Critic -> Completion` 链路。

2026-05-21 更新：`CAP-EVAL-001` 收口时先落地了一批 Planner 内部确定性快速路径；`META-009` 第一轮随后补上了 Runtime 策略层和 Planner skill 上下文分层：

- 新增 `ExecutionPolicy`：显式 `executor` / builtin `skill_name` + 结构化 payload + L2 以内 + 无网络/删除/L4/外部路径/上下文指代时，直接生成单 action TaskGraph，跳过 Planner。
- 每个 task 写入 `plan/policy_decision.json`，runner diagnostics 汇总 `policy_decision`、`skipped_stages`、`risk_flags` 和 `context_budget`。
- Planner payload 新增 `skill_capability_index`、`expanded_skill_manifests` 和 `skill_manifest_budget`；兼容字段 `skill_registry_entries` 现在等同于展开 manifest 集合。
- 当目标或 expected output 明确要求验证 Planner context / planning context 时，策略层强制回退 Planner，避免把评测对象跳过。

Critic / Completion 仍保留；本地 artifact validator、Critic / Completion 条件化跳过，以及 contract gap 后按需补 manifest + replan 仍待后续阶段完成。

## 问题定义

当前 Runtime 缺少按任务确定性、风险和输入结构分层的执行策略。

典型表现：

1. 明确 `executor=file` 且 payload 已经是可执行 `write/read/list` action 时，仍调用 Planner。
2. Planner 上下文虽然已通过 `PLANNER_SKILL_ENTRY_LIMIT` 和相关性过滤避免全量注入 active skill manifest，但还没有做到“只给能力索引，命中后再按需展开完整 contract”。
3. 成功的低风险确定性任务仍固定触发 task-level Critic 和 Completion evaluator。
4. 性能优化缺少策略记录，后续无法审计“为什么跳过 Planner / Critic / Completion”。

## 目标

在不降低复杂任务完成率和安全边界的前提下，为低风险、确定性、结构化任务建立快速路径。

阶段性目标：

1. 简单结构化任务显著减少 LLM 调用次数。
2. Planner 只接收与当前任务相关的 skill manifest 或压缩摘要。
3. Critic / Completion evaluator 根据风险和可验证证据条件化执行。
4. 所有跳过、裁剪和降级决策都写入 artifacts，便于诊断和回放。
5. Skill 信息分层：常规 Planner 请求只拿极简 capability index；只有命中候选、显式指定、contract gap 或 replan 时才展开完整 manifest / contract。

## 非目标

- 不全局关闭 Planner。
- 不全局删除 recall、workflow、vector、failure context。
- 不关闭权限系统、sandbox、execution log、artifact 记录。
- 不为了速度跳过失败、高风险、网络、L4、删除、跨任务指代和复杂多步任务的验证。
- 不用降低断言或隐藏失败来制造“变快”。

## 架构方向

新增一个明确的策略层，暂定为 `ExecutionPolicy` 或 `PlanningPolicy`，在 Runtime 创建 task 后、Planner 调用前判断本轮任务属于哪类路径。

建议策略枚举：

- `direct_execute`：结构化 payload 已可直接执行，不调用 Planner。
- `plan_required`：自然语言、复杂、多步、指代、权限不明确或缺少 payload，必须走 Planner。
- `plan_with_compact_registry`：需要 Planner，但只展开相关 skill manifest。
- `full_evaluation`：执行后必须跑 Critic + Completion。
- `local_or_light_evaluation`：确定性成功路径可用本地证据或轻量 completion 替代部分 LLM 评估。

## Fast Path 触发条件

第一阶段只允许非常保守的 `direct_execute`：

- `TaskRequest.executor != "auto"` 或 `TaskRequest.skill_name` 明确。
- `TaskRequest.payload` 非空。
- payload action 能通过对应 executor / skill contract 静态校验。
- `max_permission <= L2`。
- 不需要网络、L4 shell、删除、外部路径、审批、等待、replan。
- 不包含上下文指代：例如“刚才那个文件”“之前的结果”“同一个文件”。
- 不依赖 `workflow_matches` / `vector_matches` 才能理解目标。

不满足任何条件时，必须回退完整 Planner 路径。

## Skill Registry 预算策略

Planner 仍然需要时，skill registry 应按相关性分层：

1. 显式指定 skill：只展开该 skill 的完整 contract。
2. 显式 executor：只展开 executor 对应 builtin skill。
3. preferred skill：展开 preferred skill，其他候选只保留摘要。
4. auto 任务：保留当前过滤逻辑，但增加总字节预算和每 skill 摘要预算。
5. 如果执行前 contract gap，允许触发一次“补充 manifest 后 replan”，而不是一开始全量发送。

## Skill Manifest 按需展开计划

当前实现状态：

- `SkillLoader.load_all()` 会扫描所有 skill 的 `skill.yaml` 元数据并同步 registry。
- Planner 上下文不会默认注入所有 active manifest；`filter_skill_registry_entries_for_planner()` 会按 action、tags、executor、权限和历史信号裁剪，当前上限为 6 个。
- Dispatcher 执行 skill 时才通过 `load_runner()` import 对应 `main.py`，执行代码已经基本按需加载。
- 2026-05-21 `CAP-EVAL-001` 收口时，已局部落地一批保守确定性 Planner fast path：显式 builtin skill payload、file one-line / multi-line / field report 写入、读源文件后写报告、replace_line + append、JSON/config report、网络权限拒绝和 browser blocked payload。它们仍属于 Planner 内部 deterministic plan，不是完整 `ExecutionPolicy`。
- 2026-05-21 `META-009` 第一轮已新增 `ExecutionPolicy`，显式低风险结构化 payload 可在 Runtime 层跳过 Planner；策略决策写入 artifact 并进入真实评测 diagnostics。
- Planner request payload 已拆分 `skill_capability_index` / `expanded_skill_manifests` / `skill_manifest_budget`；旧 `skill_registry_entries` 兼容字段只保留展开 manifest，普通任务展开上限默认 2。
- task-level Critic LLM 失败已有 fallback verdict，避免真实 provider 波动导致 CLI 空 stdout；但 Critic / Completion 条件化跳过和本地 artifact validator 仍未完成。
- 仍未完成的是 `need_manifest` / contract gap 后按需补 manifest + replan 闭环，以及最终降级旧兼容字段。

目标形态：

1. `SkillCapabilityIndex`：给 Planner 的默认能力索引只包含 `skill_name`、`actions`、`tags`、`permission_rank`、`sandbox_level`、极短 description 和历史质量信号。
2. `ExpandedSkillManifest`：完整 `description`、`input_schema`、`output_schema`、`contracts` 只在以下情况展开：
   - 用户显式指定 `skill_name` 或 `preferred_skills`
   - `executor` 能唯一映射到 builtin skill
   - payload action 与某个 skill contract 精确匹配
   - Planner 初次计划后出现 contract gap，需要补充 manifest 后 replan
   - 真实评测或调试模式显式要求 full manifest
3. Planner request payload 分成 `skill_capability_index` 与 `expanded_skill_manifests`，避免把所有信息混在 `skill_registry_entries`。
4. Dispatcher 继续以 registry / contract 为准做硬校验，不能因为 Planner 只看到摘要就放宽执行约束。
5. diagnostics 汇总本轮：
   - capability index 数量和字节数
   - expanded manifest 数量、名称、展开原因和字节数
   - 是否因 contract gap 触发 manifest 补充 / replan
   - 未展开 skill 的数量和裁剪原因

分阶段落地：

1. 先改数据形状：保留兼容字段，同时新增 `skill_capability_index` / `expanded_skill_manifests`，并在 LLM call stats 中记录大小。
2. 再改 Planner prompt：明确默认只依据 capability index 选择候选；需要完整 contract 时输出 `missing_capability` 或 `need_manifest` 类结构化请求。
3. 再改 Runtime / Planner 循环：收到 `need_manifest` 后补充指定 skill manifest 并只 replan 一次，避免循环。
4. 最后删除或降级旧的 `skill_registry_entries` 全量候选字段，保留调试开关。

边界：

- 不能把未展开 manifest 作为不可用 skill；摘要只影响 Planner 可见上下文，不影响 Dispatcher 最终选择。
- 高风险、网络、L4、删除、自动生成 skill 和权限不明确任务，不能因为摘要模式而跳过权限或 contract 校验。
- 如果 capability index 不足以决策，必须补充 manifest 或回退完整 Planner，而不是让模型猜 payload。

## Critic / Completion 条件化

建议分阶段推进，避免一次性改变安全语义。

第一阶段：

- 保留 task-level Completion evaluator。
- 只对 `direct_execute` 的单节点成功任务跳过 Planner，不跳过后续验证。

第二阶段：

- 对低风险、确定性、单节点成功任务，跳过 task-level Critic。
- 失败、权限拒绝、retry/replan/degrade、高风险任务仍必须跑 Critic。

第三阶段：

- 增加本地 artifact validator。
- 当 expected output 可由本地断言验证时，Completion evaluator 可降级为轻量路径。
- 真实评测 full mode 仍可强制启用完整 Critic + Completion。

## 可观测性要求

每次策略决策必须落盘：

- `policy_decision`: `direct_execute` / `plan_required` / `plan_with_compact_registry`
- `reason`: 命中或未命中 fast path 的具体原因
- `skipped_stages`: 跳过了哪些 LLM 阶段
- `context_budget`: 各字段 `json_chars` 与裁剪结果
- `fallback`: 是否从 fast path 回退 Planner
- `risk_flags`: network / delete / l4 / context_ref / ambiguous_reference 等

这些字段应进入 task artifact，runner diagnostics 也要汇总。

## 分阶段计划

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 建立性能基线和场景指标，记录 Planner / Critic / Completion latency 与 payload 字段大小 | 已有初始证据 |
| 1 | 实现显式 `executor + payload` 低风险 fast path，直接构造单 action + finalize TaskGraph | 已首轮落地：`ExecutionPolicy` 直接生成单节点图，保留权限、sandbox、Critic 和 Completion；Planner context 显式验收场景会回退 Planner |
| 2 | Planner skill 信息分层：默认 capability index，按需展开完整 manifest / contract | 已完成数据形状首轮：payload 拆为 capability index / expanded manifests / manifest budget，`need_manifest` replan 仍待 |
| 3 | 条件化 task-level Critic，保留失败和高风险路径完整验证 | 待启动 |
| 4 | 引入本地 artifact validator，支持真实评测 fast/full 双模式 | 待启动 |
| 5 | 多模型 / tier routing：简单任务用更快模型，复杂规划用强模型 | 待排期 |

## 验收标准

必须新增或复用真实场景验证：

1. 低风险结构化 file write 场景走 fast path，并保留权限、execution log、artifact。
2. 自然语言多步任务仍走 Planner。
3. 跨任务指代、多轮上下文、网络、L4、删除、失败恢复仍走完整路径。
4. `skill_registry_entries` 在简单任务中不再占据 Planner 主要 payload。
5. diagnostics 能解释每个阶段是否被跳过及原因。
6. Planner 请求中 `skill_capability_index` 与 `expanded_skill_manifests` 分开记录；普通任务 expanded manifest 数量应小于等于 2，除非有明确展开原因。
7. contract gap 场景能触发一次按需 manifest 补充 / replan，并在 diagnostics 中留下证据。

性能目标：

- 第一阶段：结构化 file write 场景减少至少 1 次 LLM 调用。
- 第二阶段：Planner request payload 中完整 skill manifest 字节数降低到当前 30% 以下；普通任务只发送 capability index 或少量 expanded manifest。
- 第三阶段：低风险确定性任务端到端耗时相对当前 `104s` 基线显著下降，同时复杂场景无回归。

验证命令：

```bash
poetry run python -m pytest tests/
poetry run ruff check src/ tests/ scripts/run_real_world_evals.py
python3 scripts/run_real_world_evals.py --check-only
python3 scripts/run_real_world_evals.py --scenario explicit_file_payload_fast_path
python3 scripts/run_real_world_evals.py --scenario context_recall_hygiene_filters_failed_and_duplicate_memory
python3 scripts/run_real_world_evals.py --include-complex --scenario context_budget_many_memories_keeps_relevant_matches
```

真实 LLM 场景需要单独确认 provider、model 和费用风险。

2026-05-21 首轮验证记录：

- `eval_runs/policy_explicit_check/20260521_104629/`：`explicit_file_payload_fast_path` 1 passed，diagnostics 显示 `direct_execute` 且 task 目录无 `planner_v1` LLM call。
- `eval_runs/20260521_104512/`：`context_recall_hygiene_filters_failed_and_duplicate_memory` 1 passed，因 expected output 明确要求 Planner context，策略层以 `planner_context_required` 回退 Planner。
- `eval_runs/policy_complex_context_check/20260521_104738/`：`context_budget_many_memories_keeps_relevant_matches` 1 passed，同样通过 `planner_context_required` 保留 Planner 上下文验收。
- 回归：`python3 scripts/run_real_world_evals.py --check-only` 为 39 scenarios validated；`poetry run python -m pytest tests/` 为 463 passed；`poetry run ruff check src/ tests/ scripts/run_real_world_evals.py` 通过。
- 备注：一次默认全量真实基线 `eval_runs/20260521_100101/` 在旧断言口径下中途暴露 `context_recall_hygiene_filters_failed_and_duplicate_memory` 与 direct_execute 的验收对象冲突，已中止，不作为通过基线。

## 风险与边界

- Fast path 误判会降低复杂任务完成率，所以第一阶段必须严格限定在结构化、低风险、payload 完整的任务。
- 跳过 Planner 不能跳过 permission 和 sandbox。
- 跳过 Critic / Completion 必须晚于 fast path，且要有本地证据替代。
- 如果任何静态校验失败，必须回退 Planner，而不是继续硬执行。
