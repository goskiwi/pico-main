# RepoMap paid-model ablation

Date: 2026-08-18

Model: `gpt-5.6-luna`

Runtime baseline: `af3f831`

Harness branch: `codex/repomap-ablation-harness` at `77ba03f`

Tool budget: 40 per task

The experiment used the same five frozen Real OSS fixtures, prompts, Docker image and hidden
verifiers for every variant. Run order was deterministically shuffled. Provider failures were
retried at most twice and recorded separately from model outcomes.

Variants:

- `AUTO`: inject the task-ranked RepoMap and expose `query_repo_map`.
- `ON-DEMAND`: inject only a disabled marker but expose `query_repo_map`.
- `OFF`: inject the disabled marker and remove `query_repo_map` from the tool surface.

## Three-variant screening

| Variant | Passed | Tool steps | Input tokens | Output tokens | Cached tokens | Duration |
|---|---:|---:|---:|---:|---:|---:|
| AUTO | 5/5 | 71 | 1,247,125 | 11,107 | 1,090,560 | 581.4 s |
| ON-DEMAND | 5/5 | 83 | 1,580,362 | 14,462 | 1,405,312 | 730.4 s |
| OFF | 5/5 | 91 | 1,852,134 | 14,855 | 1,663,488 | 763.2 s |

`query_repo_map` was not called in any screening run. ON-DEMAND therefore provided no observed
retrieval benefit and remained more expensive than AUTO.

## AUTO versus OFF confirmation

A second repetition was run only for AUTO and OFF. The final Jinja OFF run was unavailable after
two provider disconnects, so aggregate comparison uses the nine task/repetition pairs where both
variants produced a model result.

| Variant | Paired results | Passed | Tool steps | Input tokens | Non-cached input | Output tokens | Duration |
|---|---:|---:|---:|---:|---:|---:|---:|
| AUTO | 9 | 9/9 | 133 | 2,469,784 | 303,000 | 24,370 | 1,228.4 s |
| OFF | 9 | 9/9 | 161 | 2,983,548 | 361,084 | 28,591 | 1,417.9 s |

Relative to OFF, AUTO used:

- 17.4% fewer tool steps.
- 17.2% fewer input tokens.
- 16.1% fewer non-cached input tokens.
- 14.8% fewer output tokens.
- 13.4% less wall time.

AUTO used fewer tool steps in seven of nine paired runs. Its first tool call directly read the
eventual production target file in all nine pairs; OFF did so in none, beginning with `search` or
`list_files` instead.

## Decision

The ablation does not show a success-rate improvement: all completed variants passed their hidden
verifiers. It does show a repeatable navigation and cost benefit. The predeclared retention gate
was equal quality with at least a 15% reduction in tool steps or tokens; AUTO clears that gate on
both measures.

Keep automatic RepoMap injection. Do not replace it with ON-DEMAND mode. The `query_repo_map` tool
itself remains unproven because it was never called; its separate value should be tested or the tool
surface removed without deleting automatic prompt injection.

Limitations: five tasks are not a general coding-success estimate, one paired result is missing due
to provider infrastructure, and this experiment measures coding-agent efficiency rather than PR
Review precision or recall. PR Review still needs a repository-isolated holdout with local and
cross-file findings.
