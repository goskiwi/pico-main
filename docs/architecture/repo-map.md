# Task-aware Python Repo Map

Pico's repository map is a retrieval layer, not a source-code summary. Its job is
to rank a small set of definitions for the current request before the model starts
opening files.

## Pipeline

```text
workspace Python files
  -> tree-sitter parse
  -> module/class/function/method symbols
  -> import/call/inherit/contains/test edges
  -> query lexical seeds
  -> personalized weighted PageRank
  -> lexical + graph score
  -> per-file diversity selection
  -> token-budgeted signatures
```

Each file cache entry is keyed by relative path, nanosecond modification time, and
size. A query rescans file metadata, reparses only changed files, rebuilds the
resolved graph deterministically, and computes a new task-personalized ranking.
Deleted files are evicted from the cache.

## Ranking

The query tokenizer preserves complete identifiers and also splits snake case,
paths, dotted names, and CamelCase. Exact symbol, qualified-name, path, signature,
and test-intent matches produce lexical weights. When at least one lexical seed
exists, PageRank has no global prior: relevance cannot leak into an unrelated
connected component merely because that component contains a hub.

References are resolved conservatively:

- same-file definitions win;
- exact qualified names win over simple names;
- ambiguous calls with more than four cross-file candidates are dropped;
- Python built-in calls such as `len`, `getattr`, and `range` do not create edges;
- reverse edges have lower weight so callers and tests remain discoverable without
  overpowering the direction of dependency.

The final rank combines normalized lexical and PageRank scores. Rendering applies a
per-file diversity penalty, groups signatures by path, and rejects any addition that
would cross the section's real tokenizer token budget.

## Context and tool paths

`ContextManager` injects the first ranking into a dedicated `repo_map` section.
Requests that name a file or dotted symbol borrow additional budget from older
history. During a run, `query_repo_map` can refresh the graph after edits and rank a
new sub-question without rebuilding the model's full prompt.

An explicit `repo_map_budget_tokens` runtime value changes that section from the
normal dynamically adjusted budget to a hard upper bound. Benchmark variants use
this path for 600, 1000, and 1600-token budget studies, and retain the configured cap
in artifact rows and reports.

CLI users can select the same behavior with `--repo-map-budget <positive-int>`.
Omitting the option preserves dynamic budgeting. The option affects automatic
context injection only; the read-only `query_repo_map` tool keeps its own explicit
per-call budget.

`pico repo-map --query "..."` uses the same parser, graph, ranking, and renderer
without constructing an agent, calling a model, or creating run artifacts. It
prints the selected signatures plus their lexical/graph scores and match reasons,
which makes the retrieval decision inspectable in a demo or regression triage.

Prompt metadata and the final report retain:

- graph node and edge counts;
- parsed, skipped, and parse-error file counts;
- file-cache hits and misses;
- selected paths and symbols;
- lexical score, graph score, final score, and match reasons;
- whether the ranking was truncated by result or token limits.

## Boundaries

This version deliberately supports Python only. `tree-sitter` and
`tree-sitter-python` are required runtime dependencies; there is no AST, regex, or
language-agnostic fallback. Generated output, dependencies, virtual environments,
runtime artifacts, caches, oversized files, and symlinks are excluded.

The graph is static and name-based. It does not perform type inference, dynamic
dispatch resolution, import execution, or framework-specific semantic expansion.
Those are future ranking inputs and should be evaluated against frozen retrieval
tasks before being added.
