# 标准基础能力库 v1

## 状态

- 任务 ID：`BASECAP-002`
- 状态：首批已落地
- 生效日期：2026-05-20
- 目标：在 `BASECAP-001` 的 JSON / CSV / datetime / HTTP / safe shell 基础上，补齐本地开发者高频基础能力缺口，减少 Planner 对 Python、shell 或模型常识的兜底依赖。

## 已落地能力

| Skill | 权限 | 主要 actions | 说明 |
|---|---|---|---|
| `builtin.data.sqlite` | `filesystem.read` | `list_tables` / `schema` / `query` / `export_csv` / `report` | 只读 SQLite 查询、导出和指标报告；仅允许 SELECT / WITH / PRAGMA |
| `builtin.text.markdown` | `filesystem.read` | `headings` / `extract_section` / `replace_section` / `append_section` / `table` | Markdown 标题、章节和 pipe table 的结构化处理 |
| `builtin.vcs.git` | `system.exec` | `status` / `diff` / `log` / `show` | Git 只读命令，必须走 L4 process 权限 |
| `builtin.web.html` | `filesystem.read` | `extract` / `links` / `report` | 解析已有 HTML 证据，抽取 title、正文、链接、meta 和简单表格 |
| `builtin.archive.basic` | `filesystem.read` | `list` / `extract` / `create` | zip / tar / tar.gz 列举、解压和打包，带路径穿越防护 |
| `builtin.data.config` | `filesystem.read` | `query` / `report` | TOML、简单 YAML、XML、JSON 配置字段抽取和报告 |

## 设计边界

- 新增能力默认不引入运行时依赖，优先使用 Python stdlib。
- `builtin.vcs.git` 依赖系统 `git` CLI，因此显式要求 `system.exec` 权限和 `process` sandbox。
- `builtin.web.html` 不负责联网；联网抓取仍由 `builtin.http.client` 或 `builtin.browser.basic` 完成，HTML skill 只处理已取得的可审计证据。
- `builtin.data.sqlite` 是只读能力，不支持写入、迁移、DDL 或任意 SQL 执行。
- `builtin.data.config` 的 YAML 支持是简单 mapping 子集；复杂 YAML 仍应作为后续能力扩展，不在首批引入 PyYAML 依赖。

## 验收与守护

- 每个新增 skill 都有 `skill.yaml` contracts、`README.md`、`tests/README.md` 和单元测试。
- Planner prompt 已加入新增标准 skill 的选择和 payload 示例。
- SkillRegistry planner manifest 过滤已加入 sqlite / markdown / git / html / archive / config 标签提示。
- 真实评测矩阵新增 6 个场景：
  - `sqlite_ticket_summary_report`
  - `markdown_section_update`
  - `config_toml_report`
  - `html_extract_local_report`
  - `archive_create_bundle`
  - `git_status_readonly_report`（L4 场景，默认跳过）

## 后续扩展

- 更完整的 YAML 支持需要先评估是否接受 PyYAML 运行时依赖。
- HTML 表格和正文抽取当前是 stdlib 轻量实现，不替代真实浏览器 DOM。
- Git skill 首批只读，不处理 add / commit / branch / checkout / merge 等会改变仓库状态的操作。
