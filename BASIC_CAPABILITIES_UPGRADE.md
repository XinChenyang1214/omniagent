# 基础能力升级任务

## 状态

- 任务 ID：`BASECAP-001`
- 状态：已完成
- 生效日期：2026-04-30
- 优先级：已关闭（原 P0 当前唯一主线）

`BASECAP-001` 已完成。此前因本任务设置的路线图冻结解除，后续工作恢复按 `ROADMAP.md` 和 `issue.md` 排序推进。

当前首要问题已切换为 [`CAP-EVAL-001` 复杂能力评测前置升级](./COMPLEX_CAPABILITY_EVAL_UPGRADE.md)。本文件只保留 `BASECAP-001` 的完成记录。

后续标准基础能力扩展已在 [`BASECAP-002` 标准基础能力库 v1](./BASIC_CAPABILITIES_V1_UPGRADE.md) 首批落地，覆盖 SQLite / Markdown / Git 只读 / HTML / Archive / Config。

## 最终进展

- ✅ 阶段 0：Browser 中文 URL 编码、配置安全治理、builtin skill 结构规范已落地
- ✅ 阶段 1：`builtin.data.json` / `builtin.datetime.format` / `builtin.data.csv` 已落地
- ✅ 阶段 2：`builtin.http.client` 已落地
- ✅ 阶段 3：`builtin.shell.safe` 已落地
- ✅ 阶段 4：能力选择质量升级 / `ISSUE-007` 已落地
- ✅ 新增 62 条回归测试，当前全量 `pytest` 为 346 passed
- ✅ `poetry run ruff check src/ tests/` 通过
- ✅ `ROADMAP.md` / `issue.md` 状态已更新，优先级冻结解除

## 背景

OmniAgent 的三阶段 MVP 和 P0 补丁轮已经完成，核心 runtime、Planner、ExecutionEngine、Critic、ResponseGenerator、混合记忆、Skill 生命周期和 LLM 接入都已经可运行。本专项启动时的短板不在架构闭环，而在冷启动基础能力过窄：

- active builtin skill 已从 `builtin.file.basic` / `builtin.python.basic` / `builtin.browser.basic` 扩展到包含 `builtin.data.json` / `builtin.datetime.format` / `builtin.data.csv` / `builtin.http.client` / `builtin.shell.safe`
- executor 层有 `file` / `python` / `browser` / `llm_native`，此前标准 skill 层覆盖不足
- 自动生成 skill 只能作为补位机制，不能替代稳定、可测试、可治理的基础能力库
- Planner 的能力选择依赖 skill contract，基础 skill 越薄，规划质量越受限

因此本专项先建设标准基础能力库，让系统拥有一组稳定、低风险、高频、可审计的内置能力。该专项现已完成。

## 目标结果

把薄底座升级为可持续扩展的标准能力层：

```text
底层 executor / sandbox 能力
  -> 标准 builtin skill 库
  -> contract 驱动的 Planner 选择
  -> 权限 / sandbox / lifecycle 治理
  -> 可沉淀 workflow / generated skill
```

完成结果：

- 扩展高频基础能力：JSON、CSV、datetime、HTTP、安全 shell
- 所有新增能力只使用 stdlib，延续零运行时依赖原则
- 所有 action 都通过 `skill.yaml:contracts` 明确输入、输出和副作用
- Dispatcher 能在执行前通过 contract 拦截缺字段和明显类型错误
- 每个能力都有 README 和测试覆盖
- 每批完成后全量 `pytest` 与 `ruff` 通过

## 暂停范围 ✅ 已解除

以下工作曾在 `BASECAP-001` 完成前暂停，现已恢复按路线图排序：

- P1 流式输出
- P2 智能深化，包括真实 embedding、路由本体拆分、closed-loop learning、分层召回
- P3 生产化，包括 benchmark、多模型编排、用户文档扩展、Chat 输出编排层
- 长期认知抽象方向

此前允许例外：

- 修复阻塞 `BASECAP-001` 的 bug
- 补充 `BASECAP-001` 所需测试和文档
- 修正会导致新增 builtin skill 无法注册、选择、执行或验证的问题

注意：以上仅描述 `BASECAP-001` 专项期间的历史冻结。当前 P0 冻结以 `ROADMAP.md` 和 `issue.md` 中的 `CAP-EVAL-001` 为准。

## 阶段 0：基础修补 ✅ 已完成

### 0.1 修复 Browser 中文 URL 编码

问题：`sandbox.fetch_url()` 对 HTTP/HTTPS URL 直接调用 `urlopen(url)`，中文 URL 或中文 query 可能触发 `UnicodeEncodeError`。

要求：

- 对 HTTP/HTTPS URL 的 non-ASCII 部分执行安全编码
- 保持已有 ASCII URL 行为不变
- 增加回归测试覆盖中文 path、中文 query、纯 ASCII URL

### 0.2 配置安全治理

问题：本地 `omniagent.toml` 不应保存明文 API key。

要求：

- 文档明确 API key 应通过 `OMNIAGENT_LLM_API_KEY` 注入
- 配置文件示例保留 provider/model/api_base，不保留真实 key
- 不把密钥值写入任何文档、测试或日志

### 0.3 固化 builtin skill 规范

每个 builtin skill 必须包含：

```text
src/omniagent/builtin_skills/<skill_name>/
├── skill.yaml
├── main.py
├── README.md
└── tests/
```

统一返回结构：

```python
{"success": bool, "status": str, "message": str, "output": dict}
```

`skill.yaml` 必须声明：

- `name`
- `version`
- `description`
- `input_schema`
- `output_schema`
- `permission_requirements`
- `dependency_requirements`
- `side_effects`
- `sandbox_level`
- `tags`
- `contracts`

## 阶段 1：纯 stdlib 数据能力 ✅ 已完成

这是第一批必须落地的能力，低权限、低风险、高频。

### 1.1 `builtin.data.json`

Actions：

- `validate`：校验 JSON 是否可解析
- `query`：通过 dot-path 查询字段，例如 `items.0.name`
- `format`：缩进、排序 key、压缩
- `transform`：简单 pick / rename / set / delete
- `report`：通过 dot-path 抽取多个字段并写出 `label: value` 文本报告

输入：

- `text`
- `path`
- `query`
- `format_options`
- `transform`
- `fields`
- `output_path`

输出：

- `valid`
- `value`
- `formatted`
- `errors`
- `path`

约束：

- 使用 stdlib `json`
- `text` 与 `path` 至少提供一个
- 读取文件只允许 workspace/sandbox 授权路径

### 1.2 `builtin.datetime.format`

Actions：

- `now`
- `parse`
- `format`
- `diff`
- `add`

输入：

- `value`
- `timezone`
- `input_format`
- `output_format`
- `amount`
- `unit`
- `start`
- `end`

输出：

- `value`
- `iso`
- `timestamp`
- `timezone`
- `diff`

约束：

- 使用 stdlib `datetime` 和 `zoneinfo`
- 默认 timezone 为 `UTC`
- 所有输出包含明确 timezone 或说明是 naive datetime

### 1.3 `builtin.data.csv`

Actions：

- `read`
- `select`
- `filter`
- `aggregate`
- `report`
- `write`

输入：

- `path`
- `rows`
- `columns`
- `filters`
- `aggregate`
- `metrics`
- `output_path`
- `delimiter`

输出：

- `rows`
- `columns`
- `row_count`
- `result`
- `path`

约束：

- 使用 stdlib `csv`
- 不引入 pandas
- filter 首批只支持 `equals` / `contains` / `gt` / `gte` / `lt` / `lte`
- aggregate 首批只支持 `count` / `sum` / `avg` / `min` / `max`

阶段 1 验收：

- ✅ 三个 skill 都有完整 `skill.yaml:contracts`
- ✅ 每个 skill 至少 8 条测试
- ✅ 覆盖 happy path、每个 action、缺字段、类型错误、权限拒绝或输入错误
- ✅ 全量 `poetry run python -m pytest tests/` 通过（317 passed）
- ✅ 全量 `poetry run ruff check src/ tests/` 通过

## 阶段 2：HTTP 能力 ✅ 已完成

### `builtin.http.client`

Actions：

- `get`
- `post`
- `put`
- `delete`

能力：

- headers
- query params
- JSON body
- form body
- raw body
- Bearer / Basic auth helper
- timeout
- response body 预览与截断

权限：

- 默认拒绝网络
- 必须满足 network 权限要求
- 未授权时返回明确 permission error

验收：

- ✅ network 未授权拒绝
- ✅ GET/POST 基本路径可测
- ✅ JSON body encode/decode 可测
- ✅ HTTP error body 保留摘要
- ✅ timeout 行为可测

## 阶段 3：安全 shell 能力 ✅ 已完成

### `builtin.shell.safe`

Action：

- `run`

强约束：

- 禁止 `shell=True`
- 命令必须用 argv 数组
- cwd 限定在 workspace/sandbox
- timeout 必须生效
- stdout/stderr 必须截断预览
- 只允许白名单命令：
  - `grep`
  - `find`
  - `wc`
  - `head`
  - `tail`
  - `sort`
  - `uniq`
  - `cat`
  - `ls`
  - `du`
  - `df`

权限：

- 必须满足 external command 权限要求
- 未授权时返回明确 permission error

验收：

- ✅ 白名单命令成功
- ✅ 非白名单命令拒绝
- ✅ cwd 越界拒绝
- ✅ timeout 生效
- ✅ stdout/stderr 截断稳定

## 阶段 4：能力选择质量升级 ✅ 已完成

阶段 1-3 完成后启动，现已落地。

内容：

- ✅ 解决 `ISSUE-007`：Planner 不再全量接收所有 active skill manifest
- ✅ 按 action / tags / executor_hint / required_capabilities 预过滤 skill
- ✅ Registry 打分加入 action 精确匹配、权限匹配、历史成功率、失败记录降权
- ✅ contract mismatch / permission denied / unsupported action 写入 graph memory

## 完成标准

`BASECAP-001` 完成必须满足：

- ✅ 阶段 0-4 全部完成
- ✅ 新增 5 个 builtin skill
- ✅ 新增或更新测试不少于 55 条
- ✅ 全量 `pytest` 通过
- ✅ 全量 `ruff check` 通过
- ✅ `ROADMAP.md` 更新状态
- ✅ `issue.md` 中解除优先级冻结，恢复后续路线图排序

## 当前下一步

`BASECAP-001` 已结束。该专项的完成记录到此归档；当前后续排序以 `ROADMAP.md` 中的 `CAP-EVAL-001` 为准。
