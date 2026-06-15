# Skill Promotion Spec

Last reviewed: 2026-06-13

## 文档状态

- Document type: spec
- Workstream: `META-010`
- Document status: confirmed
- Implementation status: implemented
- Authority scope: stable habit -> skill promotion candidate -> validated / active skill 的门禁、lineage、contract、权限和验证规则
- Parent workstream: [`AGENT_USABILITY_LEARNING_LOOP_MVP.md`](../workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md)

## 目标

Skill promotion 让稳定 habit 沉淀为可执行、可验证、可追溯的 skill。它不能从一次成功任务或单纯 workflow candidate 直接产生 active skill。

目标链路：

```text
ExperienceRecord
  -> ProcedureDraft
  -> VerifiedProcedure
  -> Habit
  -> SkillPromotionCandidate
  -> ValidatedSkill
  -> ActiveSkill
```

核心原则：

- No habit, no skill promotion.
- No evidence, no promotion.
- Skill 必须有 manifest、contract、权限边界、测试或验证脚本。
- 高风险 skill 不能自动激活，必须经过审批。
- 旧 workflow promotion 可以保留为迁移输入，但不能绕过 habit gate 直接创建 active skill。

## Promotion Gate

Habit -> SkillPromotionCandidate 必须满足：

| Gate | Requirement |
|---|---|
| source status | source procedure status 必须是 `habit` |
| stability | `success_count >= 3` 且 `failure_count = 0` |
| evidence | 必须保留 `source_experience_ids` 和 validator evidence refs |
| contract | 必须生成至少一个 action contract |
| permissions | manifest 必须声明所需权限；不得隐式扩大权限 |
| tests | skill package 必须包含可运行测试或验证脚本 |
| lineage | skill draft metadata 和 manifest 必须记录 experience -> procedure -> habit -> skill |

## Skill Package 要求

生成的 skill package 至少包含：

```text
skill.yaml
main.py
tests/test_skill.py
README.md
lineage.json
```

`skill.yaml` 必须包含：

- `name`
- `version`
- `input_schema`
- `output_schema`
- `permission_requirements`
- `contracts`
- `source_relation.kind = habit_promoted`
- `source_relation.habit_id`
- `source_relation.procedure_id`
- `source_relation.source_experience_ids`
- `source_relation.validator_evidence_refs`

`lineage.json` 必须可独立读取，记录：

- source experience ids
- source task ids
- procedure id
- habit id
- source targets
- validator evidence refs
- generated skill name and version
- promotion timestamp

## 执行与审批规则

- 低风险 workspace skill 可在 validation 通过后按现有审批策略自动审批和激活。
- 涉及 `network`、`code_execution`、`filesystem.delete`、`system.*`、`dependency.install` 的 skill 不得自动激活。
- source habit 的 `risk_level` 为 `medium` 或 `high` 时，即使生成包本身只声明 workspace 权限，也不得自动审批或激活。
- 当前低风险 habit promotion 支持 file（含 `list` / `read` / `write` / `append` / `replace_line` / `copy`）、JSON / JSONL data（含 `jsonl` / `ndjson` alias）、CSV / TSV data、SQLite data、config（含 TOML / YAML / XML / dotenv / INI / properties alias）、plain text、markdown、HTML、archive（含 zip / tar / gztar / tgz / tar.gz alias）和 datetime habit；更多低风险类型按真实稳定 habit 继续扩展。
- active `generated.habit.*` skill 在显式 skill + structured payload 且 contract、权限、side effect、路径和风险标记都通过静态检查时，可以由 `ExecutionPolicy` 直接执行并跳过 Planner；该 fast path 不放开 network、code execution、system exec、dependency install、delete 或未激活 skill。
- read-only Git habit 可以生成并验证为 process-backed promoted skill，但由于声明 `system.exec`、`git` dependency、`process_execution` 和 `process` sandbox，validation 通过后必须停在 `validated`，只能由用户审批后激活。
- Python habit 可以生成并验证为 code-execution-backed promoted skill，但由于声明 `code_execution` permission 和 `python_execution` side effect，validation 通过后必须停在 `validated`，只能由用户审批后激活，并在 L3 `code_execution` 权限下通过 sandbox `run_python` 执行。
- HTTP habit 可以生成并验证为 network-backed promoted skill，但由于声明 `network` 和 `network_read` / `network_write` side effects，validation 通过后必须停在 `validated`，只能由用户审批后激活。
- browser fetch habit 可以生成并验证为 network-backed promoted skill，但由于声明 `network` 和 `network_read` side effect，validation 通过后必须停在 `validated`，只能由用户审批后激活；生成 skill 使用 sandbox `fetch_url`，并保留访问验证 / captcha / access denial 的失败识别。
- safe shell habit 可以生成并验证为 process-backed promoted skill，但由于声明 `system.exec`、`process_execution` 和 `process` sandbox，validation 通过后必须停在 `validated`，只能由用户审批后激活；生成 skill 只允许白名单读向命令并使用 argv / `shell=False`。
- habit -> skill promotion 可以通过 `omniagent skill promote-habit <procedure_id>` 或统一学习治理入口 `omniagent learning promote --type habit <procedure_id>` 触发；统一入口只复用现有 promotion gate，不改变 validation、approval 或 activation 规则。
- skill validation 可以通过 `omniagent skill validate <skill_name>` 或统一学习治理入口 `omniagent learning validate --type skill <skill_name>` 触发；统一入口只复用 lifecycle validation gate，返回 validation report、approval policy 和可选 approval record，不执行 activation。
- 手动审批可以通过 `omniagent skill approve <skill_name>` 或统一学习治理入口 `omniagent learning approve --type skill <skill_name>` 完成；审批只写 approval record，不绕过后续 activation gate。
- 手动拒绝可以通过 `omniagent skill approve <skill_name> --reject` 或统一学习治理入口 `omniagent learning reject --type skill <skill_name>` 完成；拒绝写 rejected approval record，不覆盖 draft package，也不激活 skill。
- 重复 promotion 同一个 skill name 必须产生新版本，并通过普通 skill lifecycle 的 upgrade / rollback 机制管理。
- skill rollback 可以通过 `omniagent skill rollback <skill_name>` 或统一学习治理入口 `omniagent learning rollback --type skill <skill_name>` 完成；统一入口只复用 lifecycle rollback gate，不修改归档版本选择、registry 更新或 registration record 规则。
- skill 历史可以通过统一学习治理入口 `omniagent learning history --type skill <skill_name>` 查看；history 聚合 draft、validation、approval、registration 和 lineage records，不直接修改 lifecycle 状态。
- validator 或运行失败后的 skill 修复必须产生新版本，不覆盖旧 active 版本。

## CLI / API 要求

首期至少提供：

```text
omniagent skill promote-habit <procedure_id>
omniagent skill show <skill_name> --json
omniagent learning promote --type habit <procedure_id> --skill-name <skill_name>
omniagent learning validate --type skill <skill_name> --version <version>
omniagent learning approve --type skill <skill_name>
omniagent learning reject --type skill <skill_name>
omniagent learning history --type skill <skill_name>
omniagent learning rollback --type skill <skill_name> --to-version <version>
```

`skill show --json` 必须能查看 draft metadata 或等价 lineage 记录。

## 验收矩阵

| Requirement | Evidence needed | Status |
|---|---|---|
| workflow-first promotion 不能绕过 habit gate 直接创建 active skill | `tests/test_skill_auto_generation.py::AutoSkillGenerationIntegrationTest::test_workflow_promotion_requires_habit_before_skill_generation`；candidate 记录 `blocked_requires_habit` | implemented |
| stable habit 可生成 skill package | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_habit_promotes_to_active_skill_with_lineage_and_contract` 检查 `lineage.json`、validation、activation | implemented |
| skill 记录 experience -> procedure -> habit -> skill lineage | `skill show --json` 返回 `lineage_records`；`lineage.json` 记录 habit、procedure、experience 和 validator evidence | implemented |
| skill manifest 有 contract 和权限边界 | registry entry contracts 包含 `write` / `replace_line` / `copy` action；manifest 声明 `permission_requirements` | implemented |
| 低风险 habit skill 可验证并激活 | validation report passed、approval not_required、registration active，并实际执行 promoted skill 的 `write` / `replace_line` / `copy` action | implemented |
| JSON / JSONL data habit 可作为低风险类型晋升 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_json_habit_promotes_to_active_skill_and_executes_query`；`tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_jsonl_habit_promotes_as_json_skill_and_executes_line_query`；manifest 声明 `query` / `query_lines` / `report_lines` / `report` contracts，`jsonl` / `ndjson` target alias 会归入 JSON promotion，并可执行 JSON path query、JSONL line query 和 JSONL report 写入 | implemented |
| CSV / TSV data habit 可作为低风险类型晋升 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_csv_habit_promotes_to_active_skill_and_executes_aggregate`；`tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_tsv_habit_promotes_as_csv_skill_and_executes_tab_aggregate`；manifest 声明 `aggregate` / `report` contracts，`tsv` target alias 会归入 CSV promotion，并可执行 delimiter-based aggregate | implemented |
| SQLite data habit 可作为低风险类型晋升 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_sqlite_habit_promotes_to_active_skill_and_executes_query`；manifest 声明 `query` / `export_csv` / `report` contracts 并可执行只读 query | implemented |
| config / YAML / XML / dotenv / INI / properties habit 可作为低风险类型晋升 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_config_habit_promotes_to_active_skill_and_executes_query`；`tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_xml_config_habit_promotes_to_active_skill_and_executes_query`；`tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_dotenv_config_habit_promotes_to_active_skill_and_executes_query`；manifest 声明 `query` / `report` contracts，`yaml` / `yml` / `toml` / `xml` / `dotenv` / `ini` / `properties` target alias 会归入 config promotion，并可执行 YAML、XML 与 key-value config query | implemented |
| plain text habit 可作为低风险类型晋升 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_text_habit_promotes_to_active_skill_and_executes_line_extract`；manifest 声明 `stats` / `extract_lines` / `replace` / `report` contracts 并可执行按行抽取 | implemented |
| markdown habit 可作为低风险类型晋升 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_markdown_habit_promotes_to_active_skill_and_executes_section_extract`；manifest 声明 `extract_section` / `replace_section` / `table` contracts 并可执行 section extract | implemented |
| HTML habit 可作为低风险类型晋升 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_html_habit_promotes_to_active_skill_and_executes_report`；manifest 声明 `extract` / `links` / `report` contracts 并可写出 HTML summary report | implemented |
| archive habit 可作为低风险类型晋升 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_archive_habit_promotes_to_active_skill_and_executes_create`；manifest 声明 `list` / `extract` / `create` contracts，`tgz` habit target 可识别为 archive promotion，并验证 `gztar` / tar.gz 创建；同时保留 extract traversal protection | implemented |
| datetime habit 可作为低风险类型晋升 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_datetime_habit_promotes_to_active_skill_and_executes_format`；manifest 声明 `now` / `parse` / `format` / `diff` / `add` contracts，且不声明额外权限 | implemented |
| read-only Git habit 可生成但不得自动激活 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_git_habit_promotes_to_validated_skill_and_requires_manual_approval`；manifest 声明 `status` / `diff` / `log` / `show` contracts、`system.exec`、`git` dependency、`process_execution` 和 `process` sandbox；validation 后停在 `validated`，人工 approval 后才能 runtime 执行 `git status` | implemented |
| Python habit 可生成但不得自动激活 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_python_habit_promotes_to_validated_skill_and_requires_manual_approval`；manifest 声明 `run` contract、`code_execution` permission 和 `python_execution` side effect；validation 后停在 `validated`，人工 approval 后才能在 L3 权限下通过 sandbox `run_python` 执行 | implemented |
| HTTP habit 可生成但不得自动激活 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_http_habit_promotes_to_validated_skill_and_requires_manual_approval`；manifest 声明 `get` / `post` / `put` / `delete` contracts、`network` permission 和 `network_read` / `network_write` side effects；validation 后停在 `validated`，人工 approval 后才能 runtime 执行 HTTP GET | implemented |
| browser fetch habit 可生成但不得自动激活 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_browser_habit_promotes_to_validated_skill_and_requires_manual_approval`；manifest 声明 `fetch` contract、`network` permission 和 `network_read` side effect；validation 后停在 `validated`，人工 approval 后才能在显式 `allow_network` 下 runtime 执行 browser fetch | implemented |
| safe shell habit 可生成但不得自动激活 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_shell_habit_promotes_to_validated_skill_and_requires_manual_approval`；manifest 声明 `run` contract、`system.exec`、`process_execution` 和 `process` sandbox；生成 main 使用白名单命令、argv 和 `shell=False`；validation 后停在 `validated`，人工 approval 后才能 runtime 执行 `wc -l` | implemented |
| 高风险 source habit 不得自动激活 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_high_risk_habit_promotion_requires_manual_approval_before_activation`；validation 后停在 `validated`，需要人工 approval 后才能 activate | implemented |
| active 低风险 generated habit skill 可走 direct execute | `tests/test_execution_policy.py::ExecutionPolicyTest::test_low_risk_generated_habit_skill_can_direct_execute`；`test_generated_habit_skill_still_respects_permission_gate`；`test_inactive_generated_habit_skill_requires_planner`；只允许 active `generated.habit.*` 且保留权限 / contract / side effect gate | implemented |
| skill approval 纳入统一学习治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_approve_controls_procedure_and_skill_objects`；`learning approve --type skill` 复用 skill lifecycle approval policy，写 approval record，但不自动 activate | implemented |
| skill rejection 纳入统一学习治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_reject_controls_procedure_habit_and_skill_objects`；`learning reject --type skill` 复用 skill lifecycle approval policy，写 rejected approval record，但不自动 activate | implemented |
| habit -> skill promotion 纳入统一学习治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_promote_controls_procedure_and_habit_objects`；`learning promote --type habit` 复用 skill promotion gate，返回 validation、approval 和 registration evidence | implemented |
| skill validation 纳入统一学习治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_validate_controls_procedure_habit_and_skill_objects`；`learning validate --type skill` 复用 skill lifecycle validation gate，返回 previous_status、target_version、validation report、approval policy、可选 approval record 和更新后的 skill audit item，但不激活 skill | implemented |
| skill history 纳入统一学习治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_history_outputs_audit_events`；`learning history --type skill` 聚合 draft、validation、approval、registration 和 lineage 事件 | implemented |
| skill rollback 纳入统一学习治理入口 | `tests/test_learning_governance.py::LearningGovernanceIntegrationTest::test_cli_learning_rollback_controls_skill_object`；`learning rollback --type skill` 复用 skill lifecycle rollback gate，返回 previous_version、target_version、registration record 和回滚后的 skill detail | implemented |
| 重复 promotion 产生新版本 | `tests/test_skill_lifecycle.py::SkillLifecycleIntegrationTest::test_repeated_habit_promotion_upgrades_skill_version`；第二次 promotion 产生 upgrade registration | implemented |
| learning-loop 进入真实评测矩阵 | `evals/real_world/scenarios.json::learning_loop_habit_promoted_skill_lineage`；`evals/real_world/scenarios.json::learning_loop_jsonl_habit_promoted_skill_lineage`；`eval_runs/20260612_034828/` 为 1 passed / 0 failed / 0 skipped；`eval_runs/20260613_042808/` 为 1 passed / 0 failed / 0 skipped，并验证 JSONL generated habit skill 走 `direct_execute` | implemented |

## 非目标

- 不把所有 workflow candidate 自动转 skill。
- 不做可分享 skill marketplace。
- 不允许未验证 habit 触发 skill promotion。
- 不为高风险 skill 跳过审批。
