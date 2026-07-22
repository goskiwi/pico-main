# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`pico` is a lightweight local coding agent that runs in the terminal. It reads the workspace, calls a model backend, executes constrained local tools through runtime safety checks, and persists session state to `.pico/`. The package name is `pico`, the CLI command is `pico`, and the module entry point is `python -m pico`.

**Key constraint**: zero Python package dependencies in production — only Python stdlib. Docker and the prebuilt `pico-sandbox:latest` image are mandatory for `run_shell`; there is no host-shell fallback. Dev dependencies (pytest, ruff) are optional.

## Commands

```bash
# Install
uv sync

# Build the mandatory shell sandbox image (requires network only while building)
docker build -f Dockerfile.sandbox -t pico-sandbox:latest .

# Run interactively
uv run pico

# Run a one-shot task
uv run pico "fix the failing test"

# Override the OpenAI-compatible model from .env.local
uv run pico --model gpt-5.4

# Lint
uv run ruff check .

# Run all tests
uv run pytest -q

# Run a single test file
uv run pytest tests/test_tools.py -q

# Run a specific test
uv run pytest tests/test_pico.py::test_ask_single_step -q

# Opt-in remote delegate smoke; model settings stay in .env.local
PICO_RUN_LIVE_TESTS=1 uv run pytest -m live tests/test_live_delegate_smoke.py -q

# Visualize a run report
uv run python scripts/render_run_report.py .pico/runs/<run_id>

# Batch render all run reports
uv run python scripts/render_run_report.py .pico/runs --all
```

## Architecture

The main request flow is:

```
user input → pico.cli constructs Pico runtime → Pico.ask() creates task_state + run dir
→ ContextManager assembles prompt → model_client.complete() calls the model
→ parser.parse_model_output() extracts tool call or final answer
→ tool_runtime.run_tool() validates, checks permissions, gets approval, executes
→ session/task_state/trace/report written to disk
```

### Core modules

| Module | Role |
|---|---|
| `pico/cli.py` | CLI args, REPL loop, model backend selection, agent assembly (`build_agent`) |
| `pico/runtime.py` | `Pico` agent object, tool dispatch, trace helpers, and prompt state. |
| `pico/agent_loop.py` | Complete `Pico.ask()` lifecycle: model/tool loop, stop conditions, checkpoints, reports, and memory promotion. |
| `pico/models.py` | `OpenAICompatibleModelClient` (uses `/responses` API with SSE support) and `FakeModelClient` for tests. Both use stdlib `urllib`. |
| `pico/context_manager.py` | Prompt section assembly and token-budget allocation: prefix → memory → skills → relevant_memory → history → current_request. |
| `pico/context_history.py` | Transcript rendering, deterministic fallback summaries, and LLM task-graph compaction. |
| `pico/context_types.py` | Shared token estimation, semantic clipping, and `SectionRender` primitives. |
| `pico/skills.py` | Loads local `.pico/skills/**/SKILL.md`, matches skills to the current request, and renders selected guidance into prompts. |
| `pico/parser.py` | Parses model raw output into `(kind, payload)` tuples: `tool`, `final`, or `retry`. Supports both JSON-in-XML and XML-attribute tool formats. |
| `pico/tools.py` | Tool specifications, schema validation, and execution functions for 9 tools: `list_files`, `read_file`, `read_tool_output`, `search`, `run_shell`, `write_file`, `patch_file`, `delegate`, `delegate_many`. Each tool has a capability (`read`/`write`/`execute`/`delegate`) and risk level. |
| `pico/delegate_scheduler.py` | Shared bounded-concurrency and budget accounting for `delegate` / `delegate_many`; it runs read-only child agents and returns ordered outcomes. |
| `pico/sandbox.py` | Mandatory Docker execution for `run_shell`: no network, read-only rootfs, dropped capabilities, resource limits, protected workspace paths, timeout cleanup, and audit metadata. |
| `pico/tool_runtime.py` | Tool execution lifecycle: validation → permission check → approval → dry-run → workspace snapshot → execute → diff snapshot → audit. |
| `pico/tool_policy.py` | Capability checking, risk classification, read-only enforcement, repeated-call detection, shell policy lookup. |
| `pico/memory.py` | `LayeredMemory`: working state (goal, recent files), episodic notes, file summaries. `DurableMemoryStore`: persistent structured memory in `.pico/memory/` with `MEMORY.md` index. |
| `pico/workspace.py` | `WorkspaceContext`: git-aware snapshot (branch, status, recent commits, project docs). Used as the stable prefix in prompts. |
| `pico/security.py` | Secret redaction in traces/reports, shell environment filtering, sensitive env var detection. |
| `pico/config.py` | Shared budgets, limits, feature flags, allowlisted shell commands, and dangerous shell patterns. |
| `pico/checkpoints.py` | Session resume state evaluation, checkpoint creation, runtime identity comparison. |
| `pico/session_store.py` | JSON file persistence for sessions in `.pico/sessions/`. |
| `pico/run_store.py` | Per-run artifact storage in `.pico/runs/<run_id>/` (trace.jsonl, task_state.json, report.json). |
| `evaluation/real_benchmark.py` | Live-LLM benchmark manifest, model client, hidden verifier, and runner. |
| `evaluation/real_benchmark_evidence.py` | Trace accounting, delegate evidence, and workspace isolation audit. |
| `evaluation/real_benchmark_reporting.py` | Metric aggregation, artifact comparison, and Markdown reporting. |
| `pico/report.py` | Run report and tool audit log construction. |
| `pico/durable_memory.py` | Rule-based durable memory promotion/rejection logic. |
| `pico/memory_runtime.py` | Runtime hook for triggering durable memory extraction after `ask()` completes. |
| `pico/workspace_diff.py` | Workspace snapshot hashing and diff computation for tool audit. |
| `pico/approval.py` | User approval prompt for risky tool execution. |

## Key design patterns

### Tool safety chain

```
schema validation → dangerous command hard-block → shell allowlist classification
→ capability / read-only check → dry-run or approval → filtered env execution
→ trace/report audit
```

### Prompt structure

The prompt sent to the model has six sections assembled in fixed order:
1. **prefix** — stable: agent identity, tool list, workspace snapshot
2. **memory** — working state, recent files, file summaries
3. **skills** — selected local skill guidance matched to the current request
4. **relevant_memory** — episodic/durable notes matched to the current request
5. **history** — recent transcript; older entries compacted to structured summary
6. **current_request** — the user's message (never trimmed)

Token budgets are enforced per section. When the total exceeds `DEFAULT_TOTAL_BUDGET` (12000), sections are reduced in order: `relevant_memory` → `skills` → `history` → `memory` → `prefix`.

### Skills

Local skills live under `.pico/skills/**/SKILL.md`. Each file can include simple frontmatter with `name` and `description`. `ContextManager` reloads skills when building each prompt, selects up to three skills by lexical matching against the user request, and injects them as a `Skills:` section between memory and relevant memory. Selected skills are recorded in prompt metadata, trace, and report summaries.

Committed example skills live under `examples/skills/`. To enable them locally:

```bash
mkdir -p .pico/skills
cp -R examples/skills/* .pico/skills/
```

Do not commit `.pico/skills/`; `.pico/` is local runtime state. Add reusable skill templates under `examples/skills/` instead.

### Model output format

Models must return exactly one `<tool>...</tool>` or `<final>...</final>` per response. Two tool formats are supported:
- JSON in XML: `<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>`
- XML attributes: `<tool name="write_file" path="file.py"><content>...</content></tool>`

### Workspace sandbox

All file paths are resolved relative to the repo root. Path traversal (`../`) and symlink escapes are blocked. Write tools cannot modify `.git/`, `.pico/`, `.venv/`, `.env`, or similar protected paths.

### Shell execution

Only allowlisted commands can run under `--approval never`. Dangerous patterns (recursive force delete, hard git reset, curl-pipe-shell, etc.) are hard-blocked before approval. Shell commands inherit a filtered environment with only safe variables.

### Testing

The pytest suite is offline and does not measure model capability. Tests usually use `build_agent()` from `tests/helpers.py`, which wires `FakeModelClient` with scripted outputs. The standard pattern:

```python
from tests.helpers import build_agent

agent = build_agent(tmp_path, outputs=[
    '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
    "<final>Done.</final>",
])
result = agent.ask("do something")
```

## Session and run persistence

- Sessions: `.pico/sessions/<session_id>.json`
- Runs: `.pico/runs/<run_id>/` containing `task_state.json`, `trace.jsonl`, `report.json`
- Memory: `.pico/memory/entries/<type>.md` with `.pico/memory/MEMORY.md` index

These directories should not be committed (`.pico/` is in `.gitignore`).

## Feature flags

All gated behind `feature_flags` dict. The defaults enable everything:

```python
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
    "llm_memory_extract": True,
    "llm_history_compaction": True,
}
```

Tests typically disable `llm_memory_extract` and `llm_history_compaction` to avoid calling a real model during memory extraction.
