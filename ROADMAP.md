# OmniAgent 路线图

记录从当前状态出发的近期目标和长期方向。

## 当前优先级状态 (2026-05-21)

**[`CAP-EVAL-001` 复杂能力评测前置升级](./COMPLEX_CAPABILITY_EVAL_UPGRADE.md) 已完成。**

`BASECAP-001` 基础能力升级与 `REAL-EVAL-005` 第二批真实场景矩阵均已完成；进入 `REAL-EVAL-006` 复杂真实场景前置能力已补齐：

- 任务完成度评估节点
- 模型能力不足与 Agent 流程缺陷的归因切分
- 真实 embedding 接入
- 评测 diagnostics 与 failure attribution 升级

`CAP-EVAL-001` 阶段 4 已归档：默认非 Git 基线 `eval_runs/20260521_073254/` 为 27 passed / 0 failed / 11 skipped；`REAL-EVAL-006` 8 个复杂场景矩阵 `eval_runs/20260521_092801/` 为 8 passed / 0 failed / 0 skipped；`git_status_readonly_report` 按用户要求未执行。

当前已启动 `META-009` Runtime 分层执行与性能优化。第一轮已落地 `ExecutionPolicy` direct execute、策略 artifact / diagnostics，以及 Planner skill capability index / expanded manifest 数据形状。

---

## 当前状态 (2026-05-21)

已经完成的阶段：

- ✅ Phase 1 — 工程骨架、workspace-first 目录、CLI、Runtime Gateway、文件记忆
- ✅ Phase 2 — LLM Adapter、TaskGraph 模型、Planner、Critic、ExecutionEngine、Skill Registry、Workflow Memory
- ✅ Phase 3 — Interaction Decision Layer、Graph Memory、Workflow 编译、Skill 生命周期、受控自动生成、长期任务调度、分级沙箱
- ✅ **LLM 真正接入** — 移除所有 mock 硬编码智能，统一 OpenAI Chat Completions 适配器，所有决策走真实 LLM
- ✅ **自然语言回复** — 新增 `ResponseGenerator`，对话入口产出自然语言而非结构化字段
- ✅ **配置文件支持** — `omniagent.toml` + 环境变量双通道
- ✅ **P0 — 真实 LLM 验证与调优** — 多轮真实会话测试完成，prompt 调优落地，结构化输出稳定
- ✅ **真实评测诊断层** — `scripts/run_real_world_evals.py` 为每个 case 生成 `diagnostics.md`，沉淀 route / skill / 权限 / 节点 / LLM 调用 / workspace 产物证据
- ✅ **P0 补丁轮** — 8 个 META 中 6 个根治，16 个 ISSUE 中 12 个关闭，284 条回归测试全绿
- ✅ **BASECAP-001 基础能力升级** — 新增 JSON / datetime / CSV / HTTP / safe shell 标准 builtin skill，Planner skill manifest 预过滤与 registry scoring 升级
- ✅ **BASECAP-002 标准 skill 库 v1 首批** — 新增 SQLite / Markdown / Git 只读 / HTML / Archive / Config 标准 builtin skill，并补真实评测场景定义
- ✅ **REAL-EVAL-005 第二批真实场景矩阵** — 新增 10 个真实场景，覆盖错误恢复、复杂多轮、规划合理性和产物质量；最终 `eval_runs/20260512_032854/` 为 10 passed / 0 failed / 0 skipped
- ✅ **当前回归基线** — `poetry run python -m pytest tests/` 为 463 passed，`poetry run ruff check src/ tests/ scripts/run_real_world_evals.py` 通过；`poetry run python scripts/run_real_world_evals.py --check-only` 校验 39 个场景通过
- ✅ **CAP-EVAL-001 复杂能力评测前置升级** — 已完成；任务完成度评估、真实 embedding、评测 diagnostics / failure attribution 与 `REAL-EVAL-006` 复杂矩阵均已归档

**关键修复里程碑（2026-04-14 ~ 2026-04-17）**：
- 结构化 SkillContract + dispatcher 硬过滤（META-001）
- Planner decision 四出口 + LLMNativeExecutor（META-003/004）
- per-turn 召回 O(N²)→O(N) + N+1 修复（META-006）
- TaskGraph 依赖 backfill（META-007）
- 跨任务产物 pull model（META-008，runtime + Planner + ExecutionEngine 三层 + 31 条回归）
- LLM 可观测性全链路（ISSUE-001）
- 上下文召回去重、vector prompt-safe、Critic / 依赖输出 compact 与 stale task reconciliation（ISSUE-017）
- Runtime 分层执行与性能优化已启动首轮落地（META-009）
- Responder 幻觉 Python 侧三层防御（ISSUE-005）
- Critic fail verdict 强制执行（ISSUE-014）
- Responder 输出智能预览（ISSUE-016）
- 知识查询走 LLM（关键字匹配 → `knowledge_query_v1` prompt + 离线回落，10 条回归）

---

## P1 — 对话体验与可用性 🟡

**目标**：让连续对话真正可用，而不只是"能跑通"。

| # | 任务 | 状态 | 备注 |
|---|---|---|---|
| 1 | 对话历史注入 Planner | ✅ 完成 | 2026-04-14，最近 10 轮 + bootstrap 按 thread 缓存 |
| 2 | 流式输出 (SSE streaming) | ⏸️ 暂缓 | 设计已讨论（见下"流式输出设计备忘"），非 P1 阻塞项；等真实使用出现"一次性等太久"抱怨再启动 |
| 3 | LLM 调用可观测性 | ✅ 完成 | ISSUE-001，adapter → workspace 双 sink → `llm_stats` → CLI |
| 4 | 知识查询真正用 LLM | ✅ 完成 | 2026-04-17，`knowledge_query_v1` prompt + LLM 路径 + 离线关键字回落（10 条回归） |
| 5 | 错误恢复体验 | ✅ 部分完成 | LLM 超时处理、REPL 错误捕获、tier fallback 已落地 |
| 6 | **跨任务产物访问（META-008）** | ✅ 完成 | 2026-04-17，pull model 三层落地（runtime metadata 清单 + Planner `context_ref` prompt + ExecutionEngine dispatch 前解析，31 条回归） |
| 7 | **Runtime 分层执行与 fast path（META-009）** | 🟡 进行中 | 首轮已落地 `ExecutionPolicy`、策略 artifact / diagnostics、Planner capability index / expanded manifests；Critic/Completion 条件化和 artifact validator 仍待实施 |

> P1 仅剩 #2（流式输出）和 #7（Runtime fast path）。#7 已启动，下一轮优先补本地 artifact validator 与 Critic/Completion 条件化。

### 流式输出设计备忘（2026-04-17 讨论，暂缓实现）

讨论结论先记下来，哪天启动直接按这个方向落。

**首期范围**：仅 `ResponseGenerator`（chat REPL 回复滚动），UX 收益最大。`LLMNativeExecutor`（纯文本任务）作为 follow-up。Planner / Critic / Router / SkillGen 不流式 — 下游消费需要完整 JSON，流了没意义。

**关键决策（D1–D6）**：

| # | 决策 | 选项 | 备注 |
|---|---|---|---|
| D1 | 范围 | Responder 优先 | LLMNativeExecutor 作为 follow-up |
| D2 | HTTP 传输 | stdlib `urllib` + 手写 SSE 帧解析（~40 行） | 不引入 httpx，保持 HTTP 层轻量 |
| D3 | Adapter 接口 | 新方法 `generate_stream(...) -> Iterator[str \| LLMResponse]`，末位 yield 完整 `LLMResponse` | 非流式调用方零影响 |
| D4 | 结构化里流文本字段 | 轻量流式 JSON 字段提取器（状态机识别"当前在 `reply` 值里"才向 CLI emit token，整段 JSON 末尾完整解析拿 `suggested_actions`） | ~50 行 stdlib，LLMNativeExecutor 可复用（提取 `response` 字段） |
| D5 | CLI 事件通道 | 扩 `event_handlers` 新增 `TokenEvent`，复用 `_build_event_printer` 基座 | 与现有 task 事件走同一条路 |
| D6 | 开关策略 | 默认关，`--stream` / `chat.streaming=true` 打开 | 非交互场景（脚本/CI）不该流式；Ollama 本地 TTFB 反而更差 |

**实施分层（7 步）**：
1. `OpenAICompatibleAdapter._post_stream()` 内部 SSE 帧解析器
2. `LLMAdapter.generate_stream()` 抽象方法 + 实现
3. `llm/streaming.py` 下 `stream_json_field(chunks, field)` 工具
4. `ResponseGenerator.generate_streaming(context, on_token)` 新入口，`generate()` 保留为非流式
5. CLI `chat repl` 加 `--stream` flag，`TokenEvent` 接入 `event_handlers`
6. 观测：完成时一次性写 `llm_calls/*.json`，metadata 加 `"streamed": true`；不拆碎增量写
7. 回归测试：StubLLMAdapter 补 `generate_stream`（预定义 chunks）；`stream_json_field` 单测；CLI 模拟 token handler

**未决问题（动手前要定）**：
- `ResilientAdapter`（重试 + provider fallback 包装器）的流式降级语义：已吐 200 字 token，provider 崩了，切 fallback 从头重流？还是流式错了就是错了不降级？倾向后者 — 流式失败直接冒泡，caller 自己决定要不要回落到非流式。
- Provider 能力探测：Ollama / 某些自部署 provider 不保证支持 `stream: true`，需要一次性探测缓存在 `LLMAdapter` 配置里，否则每次调用都要试错。

**为什么暂缓**：当前非交互式回复延迟在 LLM 真实水平（几秒内），没有真实使用中"等太久"的抱怨。P1 其余项已全部落地，先把 benchmark 套件（P3）准备起来，有数据后再决定流式是不是值得 ~200 行实现 + SSE 帧解析 + 流式 JSON 提取器的复杂度。

---

## 已完成专项 — 基础能力升级 / 标准 skill 库 v0 ✅

**任务文档**：[`BASECAP-001` 基础能力升级](./BASIC_CAPABILITIES_UPGRADE.md)

**状态**：已完成。阶段 0 / 阶段 1 / 阶段 2 / 阶段 3 / 阶段 4 均已落地；此前路线图冻结已解除。

**目标结果**：内置能力冷启动已从"3 个薄包装 executor"升级到"5 个高频标准 builtin skill"，降低对 skill 自动生成质量的依赖。

**背景**：专项启动时底座只有 `builtin.file.basic` / `builtin.python.basic` / `builtin.browser.basic` 3 个 skill + 4 个 executor（browser 仅 `urlopen` 无 DOM；python 默认无网络）。自动生成只覆盖 4 类（file/python/browser/api），DB / 邮件 / shell / 数据格式转换等完全没有路径。冷启动用户能做的事很窄。专项选择方向 C（铺标准 skill 库）：用版本化的高频 skill 替代"等自动生成"的赌博式路径。

| # | Skill | 状态 | 权限 | 实现要点 | 批次 |
|---|---|---|---|---|---|
| 0 | Browser 中文 URL 编码 + 配置安全 + builtin skill 规范 | ✅ 完成 | 按项 | URL non-ASCII 编码、API Key env 注入文档、本地 key 行移除、结构规范测试 | 批 0 |
| 1 | `builtin.data.json` | ✅ 完成 | filesystem | stdlib `json` + 自写 dot-path 查询；actions: `query` / `transform` / `validate` / `format` / `report` | 批 1 |
| 2 | `builtin.datetime.format` | ✅ 完成 | 无 | stdlib `datetime` + `zoneinfo`；actions: `now` / `parse` / `format` / `diff` / `add` | 批 1 |
| 3 | `builtin.data.csv` | ✅ 完成 | filesystem | stdlib `csv`；actions: `read` / `write` / `filter` / `select` / `aggregate` / `report` | 批 1 |
| 4 | `builtin.http.client` | ✅ 完成 | `allow_network` (L3+) | stdlib `urllib.request` + 自写 auth helper（Bearer/Basic）；actions: `get` / `post` / `put` / `delete`；body 支持 JSON/form/raw | 批 2 |
| 5 | `builtin.shell.safe` | ✅ 完成 | `allow_external_commands` (L4+) | `subprocess` + 白名单（`grep`/`find`/`wc`/`head`/`tail`/`sort`/`uniq`/`cat`/`ls`/`du`/`df`）+ timeout + cwd 限制；actions: `run` | 批 3 |

**设计硬约束**：
1. builtin skill 本身只用 stdlib（运行时已有默认 embedding 推理依赖，但不应污染 skill 实现）
2. 统一返回结构 `{success, status, message, output}`（和 `file.basic` 一致，dispatcher 已预期）
3. 每个 action 在 `skill.yaml` contracts 里给 full schema（Planner 精准选择 + dispatcher 硬过滤依赖）

**文件布局**（对齐 `file.basic`）：

```
src/omniagent/builtin_skills/<skill_name>/
├── skill.yaml          # 名称、版本、input/output schema、contracts[]
├── main.py             # def run(payload, context) -> {success, status, message, output}
├── tests/              # 至少覆盖：happy path + 每个 action + 权限拒绝 + 错误输入
└── README.md           # 一页纸：用途 + 3 个典型 payload 示例
```

**完成批次**：

- **批 1**：`data.json` + `data.csv` + `datetime.format`（已完成，新增 33 条回归）— 无权限依赖，纯 stdlib 计算
- **批 2**：`http.client`（已完成，新增 13 条回归）— 权限敏感，需 auth helper
- **批 3**：`shell.safe`（已完成，新增 12 条回归）— 白名单 + subprocess sandbox 对齐
- **批 4**：能力选择质量升级 / `ISSUE-007`（已完成，新增 4 条回归）— skill manifest 预过滤 + registry scoring 改进

实际新增 62 条回归。完成后全量 `pytest` 为 346 passed，`ruff check` 通过。

**验收结果**：
- 每个新增 skill 均覆盖 happy path、核心 action、权限拒绝或错误输入
- `skill.yaml` contracts 按 action 声明输入、输出和副作用
- 每个新增 skill 均包含 README payload 示例

**后续扩展方向（v2+，进 P3）**：更完整 YAML、DOM/浏览器自动化、PDF/Office、邮件/日历、更多数据库方言等。

---

## 已完成专项 — 标准 skill 库 v1 首批 / BASECAP-002 ✅

**任务文档**：[`BASECAP-002` 标准基础能力库 v1](./BASIC_CAPABILITIES_V1_UPGRADE.md)

**状态**：首批已落地。目标是补齐 `BASECAP-001` 后仍缺失的本地开发者高频基础能力，减少 Planner 对 Python、shell 或模型常识的兜底依赖。

| Skill | 状态 | 权限 | 说明 |
|---|---|---|---|
| `builtin.data.sqlite` | ✅ 完成 | filesystem | 只读 SQLite list/schema/query/export/report |
| `builtin.text.markdown` | ✅ 完成 | filesystem | headings / section / table / section edit |
| `builtin.vcs.git` | ✅ 完成 | L4 `system.exec` | git status/diff/log/show 只读 |
| `builtin.web.html` | ✅ 完成 | filesystem | 解析已有 HTML 的 title/text/links/meta/table |
| `builtin.archive.basic` | ✅ 完成 | filesystem | zip/tar list/extract/create，路径穿越防护 |
| `builtin.data.config` | ✅ 完成 | filesystem | TOML / 简单 YAML / XML / JSON query/report |

真实评测矩阵新增 6 个 BASECAP-002 场景；其中 git 场景为 L4 默认跳过。

---

## 当前专项 — 复杂能力评测前置升级 / CAP-EVAL-001 🔴

**任务文档**：[`CAP-EVAL-001` 复杂能力评测前置升级](./COMPLEX_CAPABILITY_EVAL_UPGRADE.md)

**状态**：已完成。阶段 0 / 阶段 1 / 阶段 2 / 阶段 3 / 阶段 4 均已归档。

**目标**：让下一批复杂真实评测能够可靠区分：

- Agent 流程、规划、工具、记忆或完成度评估缺陷
- 模型在充分上下文下仍无法完成推理或判断的能力限制
- 环境、provider、测试预期或评测断言问题

**阶段**：

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 文档与优先级冻结 | 已完成 |
| 1 | 任务完成度评估节点与 completion artifact | 已完成 |
| 2 | OpenAI-compatible / local_model 真实 embedding + hash fallback | 已完成 |
| 3 | 真实评测 diagnostics / failure attribution 升级 | 已完成 |
| 4 | `REAL-EVAL-006` 复杂真实场景矩阵 | 已完成：8 个复杂场景最终通过 |

**当前后续**：P1 / P2 / P3 冻结解除。`META-009` 已启动，短期继续补策略层 validator、条件化验证和按需 manifest replan。

---

## P2 — 智能深化 🟢

**目标**：从"能工作"到"工作得好"。

`CAP-EVAL-001` 已完成。P2 项恢复待排期；真实 embedding 已在当前专项阶段 2 完成。

| # | 任务 | 状态 | 备注 |
|---|---|---|---|
| 1 | 真实 embedding 替换 hash 向量 | ✅ 已在 `CAP-EVAL-001` 阶段 2 完成 | 默认保留 local hash；真实 provider 可走 OpenAI-compatible `/embeddings` 或进程内 `local_model` ONNX，vector entry 记录 provider/model/dimensions/version/fingerprint |
| 2 | Critic 从历史学习 | ❌ 未开始 | 关联 META-005（closed-loop learning） |
| 3 | Skill 生成质量提升 | ❌ 未开始 | 生成后用真实样例完整流程，失败自动重生成 |
| 4 | Graph memory 查询增强 | ❌ 未开始 | 路径查询、k-hop、相似度传播 |
| 5 | 多步推理链 | ❌ 未开始 | Planner chain-of-thought + Critic 读 reasoning |
| 6 | 分层召回（ISSUE-003） | ❌ 未开始 | 前置依赖已清，按置信度三层路由 |
| 7 | 路由本体拆分（META-002） | ❌ 未开始 | `UserInputType` → 多维 `InputClassification` |
| 8 | Token 裁剪策略（ISSUE-004） | ❌ 未开始 | 触发后再做 |

---

## P3 — 生产化 🔵

**目标**：从"开发者能用"到"其他人也能用"。

`CAP-EVAL-001` 完成前，P3 项暂停；benchmark 扩充只保留当前基线维护，以及 `CAP-EVAL-001` / `REAL-EVAL-006` 明确需要的复杂专项场景。

1. **Benchmark / 真实场景评测套件** — 初始自主评测闭环已落地并完成默认基线、扩展 HTTP / L4 safe shell、多轮对话和 `REAL-EVAL-005` 第二批矩阵；当前在 `CAP-EVAL-001` 阶段 4 启动 `REAL-EVAL-006`，用于验证 completion evaluation、真实 embedding 和模型能力归因
2. **Runtime 分层执行与性能优化** — `META-009` 已建专项文档；简单结构化任务先走 fast path，复杂任务保留完整 Planner / Critic / Completion
3. **多模型编排** — 简单任务用便宜模型，复杂规划用强模型，per-stage 配置
4. **用户文档** — 5 分钟上手、cookbook、故障排查
5. **Chat 输出编排层** — 默认只显示自然语言回复，`--verbose` 显示事件流
6. **标准 skill 库 v2+** — 在 `BASECAP-002` 首批基础上扩更完整 YAML、DOM/浏览器自动化、PDF/Office、邮件/日历等；接受社区贡献；官方规范化

---

## 长期愿景 — 以人类认知为模板抽象 ⭐

**这不是一个可以在某个阶段完成的任务，而是 P3 之后的长期北极星方向。**

### 对应人类认知的维度

1. **记忆分层** — 工作记忆 / 情景记忆 / 语义记忆 / 程序性记忆（当前已有雏形）
2. **双过程推理** — System 1（规则路由/缓存命中）+ System 2（LLM 规划/critic）
3. **注意力与选择性召回** — 动态 context 压缩，按相关性裁剪
4. **元认知与自我模型** — agent 对自身能力的稳定认知，低把握时主动澄清
5. **技能形成曲线** — 生疏→熟练→自动化，影响推理深度和验证严格度
6. **身份连续性** — 跨会话一致人格，长期互动塑造画像
7. **社会认知** — 理解用户语境/情绪/熟练度，调整沟通方式

### 实施原则

- 概念先行，增量迁移，可测量，不教条
- 认知科学是参考不是约束，工程合理性优先

---

## 当前不做的事（明确拒绝）

- ❌ Web UI / Desktop App — CLI 优先
- ❌ 多用户 / 多租户 — 单用户本地 runtime
- ❌ 分布式执行 — 单机足够
- ❌ Docker 级容器隔离 — venv 够用
- ❌ 无节制预置内置 skill — 核心能力优先，维护高频标准库 v0（见 `BASECAP-001`），广度留给 workflow 学习 + 自动生成 + v1+

## 决策优先顺序

1. **`CAP-EVAL-001` 已完成**，后续优先级重新按真实使用痛点排序
2. 保持轻量依赖原则；默认依赖仅服务核心 runtime 和本地 embedding
3. 能力层变更必须跑全量 `pytest` 和 `ruff check`
4. P1/P2/P3/长期方向在 `CAP-EVAL-001` 完成后再恢复待排期，下一轮应基于真实使用痛点重新排序
