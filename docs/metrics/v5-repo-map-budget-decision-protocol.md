# V5 Repo Map budget decision protocol

This protocol is frozen before the first live-model run over
`benchmarks/real_world_tasks_v5.json`. V5 uses a new `ops_center` fixture and five
tasks that did not inform the V4 budget screen or confirmation.

## Fixed run

- compare `full` with `repo_map_600`;
- use the model configured in the repository `.env.local`;
- temperature 0 and three repetitions over all five tasks;
- require a clean committed worktree;
- keep the task, fixture, verifier, model settings, step budgets, and runtime fixed
  throughout the run.

## Default-change gate

Change the default automatic Repo Map behavior to a 600-token hard cap only if every
condition below is true in the first complete V5 run:

1. `repo_map_600` passes at least 13 of 15 attempts;
2. `repo_map_600` passes at least as many attempts as `full`;
3. reported input-plus-output tokens per passing attempt for `repo_map_600` are at
   least 5% lower than for `full`;
4. neither variant records a model failure, a `model_error` failure category, an
   action rejection, or a workspace-isolation failure;
5. all 15 `repo_map_600` run reports record an effective Repo Map section budget no
   greater than 600 tokens.

If any condition fails, keep the dynamic default. A provider or infrastructure
failure makes the decision inconclusive; it does not count as evidence for changing
the default. No runtime or ranking change may be made after inspecting V5 outcomes
and then claimed as part of this held-out run.

## Interpretation

Five repeated tasks remain a small repository micro-benchmark. Passing this gate
would justify the project default for the evaluated Python localization workload,
not a universal claim across repositories, languages, models, or providers.
