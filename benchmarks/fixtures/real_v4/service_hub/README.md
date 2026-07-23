# Service Hub

Small multi-package backend used for repository-localization benchmarks.

The active product code lives under `service_hub/`. The `legacy/` and
`experiments/` packages intentionally contain similar names but are not imported by
the public flows. Public smoke tests cover only the straightforward cases; benchmark
verification is injected after the coding agent stops.
