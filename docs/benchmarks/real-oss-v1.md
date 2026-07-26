# Real OSS V1 smoke benchmark

`real_oss_v1` is a small, frozen smoke suite made from three real upstream
Python bug reports. It complements the repository's synthetic localization
fixtures; it does not replace them or make a general coding-capability claim.

The suite currently includes Pydantic issue 13215, pytest issue 13974, and
Click issue 3487. Each task has an upstream repository URL, an exact pre-fix
commit SHA, a public task prompt, and a standalone hidden verifier. The exact
sources and preflight evidence are listed in
[the candidate freeze](real-oss-v1-candidates.md).

## Materialize source fixtures

The upstream repositories are intentionally not committed to Pico. Materialize
their shallow, pre-fix source trees first:

```bash
uv run python scripts/materialize_real_oss_v1.py
```

The command fetches exactly one commit per task, verifies its SHA and required
paths, removes `.git`, and records a tree digest in
`artifacts/real-oss-fixtures/.real_oss_v1.materialization.json`. Removing Git metadata is
intentional: an Agent must not discover the later fix through local history.

To recreate one existing fixture, use its exact task ID and explicitly opt into
replacement:

```bash
uv run python scripts/materialize_real_oss_v1.py \
  --task click_empty_bytes_echo --replace
```

## Build the offline task image

The source checkouts have dependencies that are not part of Pico's minimal
default sandbox image. Build the fixed V1 image before running a task:

```bash
docker build -f Dockerfile.real-oss-v1 -t pico-real-oss-v1:latest .
```

This image contains the runtime dependencies needed by all three source
checkouts. Agent and verifier commands still execute with Docker networking
disabled.

## Run the minimal external A/B

After materialization and image build, run the same frozen task set in `full` and
`no_repo_map`. The two variants share model configuration, prompts, task snapshots,
tools, step budgets, sandbox limits, and hidden verifiers. `no_repo_map` disables both
the automatic Repo Map prompt section and the read-only `query_repo_map` tool.

```bash
uv run python scripts/run_real_world_benchmark.py \
  --benchmark-path benchmarks/real_oss_v1.json \
  --sandbox-image pico-real-oss-v1:latest \
  --variant full \
  --variant no_repo_map \
  --repetitions 1 \
  --require-clean-worktree \
  --artifact-path artifacts/real-oss-v1-repo-map-ablation-1x.json \
  --report-path artifacts/real-oss-v1-repo-map-ablation-1x.md
```

The checked-in 1× artifact was captured at commit `622a797` with `gpt-5.6-luna`:
both variants passed 3/3 hidden verifiers, while `full` used 12.33 average tool
steps and `no_repo_map` used 19.00. This is an exploratory external replication,
not a statistically stable success-rate, latency, or cost comparison. In particular,
one `full` Pydantic request had a 276.94-second provider long tail.

Do not report a score unless the command runs from a clean worktree and the artifact
records matching fixture and evaluation snapshot IDs. The runner injects and runs the
hidden verifier only after Pico stops; it does not configure `--verify-cmd`. The
explicit runtime-verification gate is a separate runtime feature, with its real-OSS
evidence recorded in
[the runtime-verification smoke report](../metrics/runtime-verification-real-oss-smoke-pytest13974.md).

The three tasks are intentionally kept as a small external sanity suite. Any claim
about a performance difference requires a new, larger frozen task set and repeated
runs; do not tune the runtime against this suite and then present it as held out.

The larger ten-repository successor is [Real OSS V2](real-oss-v2.md). V1's
manifest and checked-in 1× artifact remain unchanged so its historical result
is not mixed with the new suite.
