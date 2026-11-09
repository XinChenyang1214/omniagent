# Claude Code 项目入口

本文件只提供兼容入口，避免在多个 AI 助手指南中复制并逐渐漂移同一组规则。

在本仓库工作时必须先读取：

1. [`AGENTS.md`](AGENTS.md)：项目规则、模块边界、命令、安全与验证要求。
2. [`docs/README.md`](docs/README.md)：文档区域和阅读顺序。
3. [`docs/specs/README.md`](docs/specs/README.md)：当前实现规格。
4. [`ROADMAP.md`](ROADMAP.md) 与 [`issue.md`](issue.md)：当前优先级和未关闭问题。

常用验证：

```bash
poetry run python -m pytest tests/
poetry run ruff check src/ tests/ scripts/run_real_world_evals.py
poetry run python scripts/run_real_world_evals.py --check-only
```

不要使用 `python -m unittest discover -s tests`；它不会自动加载
`tests/conftest.py` 注册的 test LLM provider。

归档文档和 completed workstream 只提供历史背景，不覆盖当前规格。
