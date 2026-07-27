---
name: run-artifact-audit
description: Audit a completed Pico run from trace, report, task canvas, phases, and saved tool references without changing the workspace. 中文: 运行审计, trace, 报告, 任务画布, 阶段, 证据.
when_to_use: Use when asked why a run stopped, whether evidence is complete, whether a canvas folded correctly, or whether a result can be trusted.
when_not_to_use: Implementing a source-code change, reproducing a live bug, or reviewing a diff that has no Pico run artifacts.
tools: [list_files, read_file, search, query_repo_map, read_task_canvas, read_task_event, read_tool_output]
allowed_tools_strict: true
priority: 100
conflicts_with: [debugging, test-driven-development, runtime-invariants]
---

# Run artifact audit

This is a read-only audit. Do not change source files, reports, or run
artifacts.

1. Identify the requested run and inspect `report.json` and `trace.jsonl`.
2. Verify a coherent lifecycle: run start, prompt construction, model actions,
   tool records, checkpoints, and exactly one terminal outcome.
3. Cross-check every material tool claim against its saved reference under
   `refs/`, not only a clipped trace preview or the final answer.
4. For a folded canvas, inspect the active `task.mmd`, the phase index, and the
   referenced `phases/phase_XXX.mmd` files. Confirm the active canvas retains
   an archive entry and each old step remains reachable.
5. Report evidence gaps explicitly: missing reference, unpaired model call,
   truncated trace, contradictory status, or unverifiable final claim.

Return a short verdict: `complete`, `incomplete`, or `contradictory`, followed
by the concrete artifact paths that support it.
