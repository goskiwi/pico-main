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
`artifacts/real-oss-fixtures/.materialization.json`. Removing Git metadata is
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

## Run a live-model smoke evaluation

After materialization and image build, use the existing real benchmark runner:

```bash
uv run python scripts/run_real_world_benchmark.py \
  --benchmark-path benchmarks/real_oss_v1.json \
  --sandbox-image pico-real-oss-v1:latest \
  --variant full \
  --repetitions 1 \
  --artifact-path artifacts/real-oss-v1-smoke.json \
  --report-path artifacts/real-oss-v1-smoke.md
```

Do not report a score until this command is run from a clean worktree and the
artifact records matching fixture and evaluation snapshot IDs. The first run is
a smoke check; grow the suite before interpreting an A/B difference.
