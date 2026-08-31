# Project Memory extension

This directory preserves Pico's project-scoped Markdown memory store outside the
core runtime. Markdown cards are durable source records; `MEMORY.md` is a
generated catalog.

The extension is intentionally **not packaged, auto-discovered, or loaded by
`Pico`**. Importing `pico` does not create memory directories, inject a memory
catalog into prompts, or register memory tools. Any future integration must be
explicit and must continue treating saved memory as untrusted historical data.

Run its standalone tests from the repository root:

```bash
uv run pytest -q extensions/project_memory/test_store.py
```
