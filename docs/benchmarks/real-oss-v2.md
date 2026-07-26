# Real OSS V2 benchmark

`real_oss_v2` is a frozen ten-task external benchmark built from historical
Python bugs in ten separate upstream repositories. It retains V1's three
tasks and adds tomlkit, tqdm, packaging, Werkzeug, more-itertools, Jinja, and
urllib3. It is a larger evaluation set, not an Agent-runtime feature.

V1 remains unchanged as the historical three-task smoke benchmark behind the
checked-in 1x artifact. V2 has no checked-in real-model score yet: do not
combine V1's 3/3 result with V2 or represent the candidate preflight as Agent
success.

Every V2 task records an exact pre-fix SHA, public issue or PR, public prompt,
and standalone hidden verifier. The Agent receives a fresh source checkout and
the public prompt only. The verifier is copied into the workspace only after
the Agent stops. Candidate provenance and preflight results are recorded in
[the V2 candidate freeze](real-oss-v2-candidates.md).

## Materialize source fixtures

The upstream checkouts are intentionally untracked. Materialize the exact,
shallow pre-fix trees before evaluating:

```bash
uv run python scripts/materialize_real_oss_v1.py \
  --manifest benchmarks/real_oss_v2.json \
  --replace
```

The command verifies every SHA and required source path, removes `.git`, adds
only manifest-declared generated version files, and writes per-suite provenance
to `artifacts/real-oss-fixtures/.real_oss_v2.materialization.json`. This keeps
the V2 record separate from V1's materialization digest and prevents a later
fix commit from leaking through local Git history.

## Build the offline image

```bash
docker build -f Dockerfile.real-oss-v2 -t pico-real-oss-v2:latest .
```

The image pins the small set of runtime dependencies needed by the ten source
checkouts. Both Agent tools and hidden verifiers run with Docker networking
disabled.

## Run an external evaluation

Use a clean worktree and fresh benchmark workspace. Start with one repetition
only as a smoke; use repeated, identical runs before reporting a comparison.

```bash
uv run python scripts/run_real_world_benchmark.py \
  --benchmark-path benchmarks/real_oss_v2.json \
  --sandbox-image pico-real-oss-v2:latest \
  --variant full \
  --variant no_repo_map \
  --repetitions 1 \
  --require-clean-worktree \
  --artifact-path artifacts/real-oss-v2-repo-map-ablation-1x.json \
  --report-path artifacts/real-oss-v2-repo-map-ablation-1x.md
```

For any success-rate, latency, or cost statement, increase repetitions and
publish the artifact with the model identifier, fixture snapshot ID, evaluation
snapshot ID, per-task results, tool counts, changed-file counts, and failure
classes. `full` and `no_repo_map` must use the same tasks, prompts, model
configuration, sandbox image, step budgets, and verifiers.

This suite is now a visible regression set because it informed evaluation
engineering. It is credible evidence of reproducibility, not a held-out or
general coding-capability benchmark.
