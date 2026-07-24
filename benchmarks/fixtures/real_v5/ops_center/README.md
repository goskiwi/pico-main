# Ops Center fixture

This synthetic repository exposes five public workflows under `ops_center`.
Production entrypoints live in each domain's `api.py`. The `legacy/` and
`experiments/` packages are inactive distractors and must not be wired into the
public flows.
