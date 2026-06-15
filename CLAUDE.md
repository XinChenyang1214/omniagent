# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OmniAgent is a task-centric local agent runtime (Phase 3 MVP). It provides a closed loop from multi-turn conversation input through task planning, execution, memory, and skill management. Single-user, single-machine, CLI-first, developer-facing.

Primary language: Python 3.11+. Package manager: Poetry. No external runtime dependencies (pure stdlib + optional LLM providers).

## Commands

```bash
# Install dependencies
poetry install

# Run all tests
poetry run python -m pytest tests/

# Run a single test file
poetry run python -m pytest tests/test_mvp_runtime.py

# Run a single test
poetry run python -m pytest tests/test_mvp_runtime.py::TestMVPRuntime::test_method_name

# NOTE: do NOT use `python -m unittest discover -s tests` — `tests/conftest.py`
# is a pytest-only convention and unittest won't auto-load it, which leaves
# the `test` LLM provider unregistered and causes 40+ false errors. Always use
# pytest for running the suite.

# Lint
poetry run ruff check src/ tests/

# Format
poetry run ruff format src/ tests/

# CLI help
poetry run omniagent --help

# Run a task (requires LLM provider configured)
OMNIAGENT_LLM_PROVIDER=openai OMNIAGENT_LLM_MODEL=gpt-4o-mini OMNIAGENT_LLM_API_KEY=<your-api-key> \
  poetry run omniagent --workspace ./workspace run "读取工作区文件" \
  --executor file --payload '{"action":"list","path":"."}'

# Interactive chat REPL
poetry run omniagent --workspace ./workspace chat repl --max-permission L2

# LLM health check
poetry run omniagent --workspace ./workspace llm check
```

## Architecture

### Execution Flow

```
User Input (CLI / Chat REPL)
  → InteractionRouter (intent classification)
  → RuntimeGateway (orchestrates all subsystems)
  → Planner (generates TaskGraph DAG)
  → ExecutionEngine (executes nodes sequentially)
    → SkillDispatcher (selects skill or executor)
    → Executors (file / python / browser)
    → Sandbox (workspace / process / container isolation)
  → Critic (verifies execution quality)
  → WorkflowCompiler (learns from success → skill candidates)
  → SkillLifecycleManager (draft → validated → approved → active)
```

### Key Modules

- **`gateway.py`** — `RuntimeGateway`: main orchestrator, wires all subsystems together. Entry point for both CLI task submission and conversation routing.
- **`runtime.py`** — `EmbeddedRuntime`: core task execution logic, bridges planner → engine → critic.
- **`execution_engine.py`** — `ExecutionEngine`: walks the TaskGraph DAG node-by-node, handles failures, replan, and approval checkpoints.
- **`models.py`** — All data models: `TaskRequest`, `Task`, `TaskGraph`, `TaskNode`, `ConversationThread`, `ConversationTurn`, `PendingCheckpoint`, skill/workflow types. Node types: `ACTION`, `APPROVAL`, `DECISION`, `WAIT`, `REPLAN`, `FINALIZE`.
- **`interaction.py`** — `InteractionRouter`: classifies user input into types (`new_task`, `task_update`, `approval_response`, `status_query`, etc.) and routes to the right handler.
- **`dispatcher.py`** — `SkillDispatcher`: skill selection strategy (explicit → registry → executor hint → auto-generate → builtin default).
- **`planner.py`** — Converts natural language goals into `TaskGraph` DAGs via LLM.
- **`critic.py`** — Post-execution verification of results.
- **`storage.py`** — `SQLiteStore`: persistent storage for tasks, events, threads, permissions, skill lifecycle records.
- **`graph_memory.py`** — `GraphMemoryStore`: entity-relationship graph (tasks, skills, threads, decisions).
- **`hybrid_memory.py`** — Unified retrieval across File + Graph + Vector memory layers.
- **`skill_lifecycle.py`** — `SkillLifecycleManager`: manages skill states (draft → validated → approved → active → deprecated), versioning, rollback.
- **`skill_auto_generation.py`** — Auto-generates missing skills (file/python/browser/api types only) with requirement analysis → code synthesis → validation → sandbox test → critic → approval.
- **`workflow_compiler.py`** — Detects repeated successful workflows and compiles them into skill candidates.
- **`sandbox.py`** — Three backends: `workspace` (direct), `process` (isolated temp dir), `container` (Python venv).
- **`permissions.py`** — L1–L5 permission hierarchy controlling network, deletion, external commands, dependency install, memory read.
- **`config.py`** — Environment-variable-based configuration (all `OMNIAGENT_*` prefixed).
- **`cli.py`** — CLI entry point (`omniagent` command), subcommands: `run`, `task`, `chat`, `skill`, `workflow`, `scheduler`, `llm`.

### LLM Adapter (`src/omniagent/llm/`)

- `openai_compatible_adapter.py` — Unified adapter for any OpenAI Chat Completions-compatible API (OpenAI, Deepseek, Qwen, Volcengine Ark, Ollama/vLLM)
- `resilient_adapter.py` — Fallback/retry wrapper
- `factory.py` — Provider creation with `register_test_provider()` hook for tests

All LLM calls use the standard `/v1/chat/completions` endpoint. Structured output uses a 3-tier fallback: json_schema → json_object → schema-in-prompt.

Provider is set via `OMNIAGENT_LLM_PROVIDER` env var (any value accepted, e.g. `openai`, `ark`, `deepseek`, `ollama`). Tests use `OMNIAGENT_LLM_PROVIDER=test` with a `StubLLMAdapter` registered in `tests/conftest.py`.

### LLM Prompts (`src/omniagent/prompts/`)

- `planner_v1.md` — TaskGraph DAG generation instructions
- `critic_v1.md` — Execution result evaluation and recovery strategy
- `router_v1.md` — User input intent classification
- `skill_generator_v1.md` — LLM-based skill code generation

### Executors (`src/omniagent/executors/`)

Pluggable execution backends: `file.py`, `python.py`, `browser.py`. All implement `BaseExecutor`.

### Workspace

Runtime workspace at `./workspace/` is auto-bootstrapped on first run. Each task gets `workspace/tasks/<task_id>/` with subdirs for plan, nodes, artifacts, critic results, and LLM call logs. Bootstrap docs (`AGENT.md`, `USER.md`, `POLICY.md`, `TOOLS.md`) are injected as context.

## Project Documents

The current project document order is defined in `AGENTS.md`:
- `README.md` — entrypoint, capability overview, quick start
- `docs/doc-registry.md` — document authority, lifecycle status, and replacement map
- `docs/product-architecture.md` — target product architecture and learning loop
- `docs/workstreams/active/AGENT_USABILITY_LEARNING_LOOP_MVP.md` — current META-010 implementation workstream
- `docs/specs/agent-state.md` — Agent state data contract and acceptance criteria
- `docs/specs/experience-record.md` — experience record schema and learning gate
- `docs/specs/procedure-memory.md` — procedure and habit lifecycle rules
- `docs/specs/skill-promotion.md` — habit to skill promotion gate, lineage, contracts, and permissions
- `ARCHITECTURE.md` — legacy implementation architecture and migration input
- `ROADMAP.md` — roadmap and priorities
- `issue.md` — open issues and recently closed records
- `REAL_WORLD_EVAL_PLAN.md` — real-world autonomous eval loop
- `CONFIGURATION.md` — configuration reference

## Code Conventions

- Ruff for linting and formatting: line length 100, target Python 3.11
- Source in `src/omniagent/`, tests in `tests/`
- Tests use `unittest` style (some also work with pytest)
- Task results described in Chinese (per `AGENTS.md`)
- Modifications should respect existing module boundaries (directory = module boundary)
- Prefer minimal changes; avoid cross-module modifications without explicit plan
