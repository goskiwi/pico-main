# Runtime verification real-OSS smoke: pytest #13974

## Result

On clean runtime commit `3e884eccac9d10e0b62436f282ea73db24d9bd48`, one live-model
attempt solved the frozen pre-fix pytest fixture for
[issue #13974](https://github.com/pytest-dev/pytest/issues/13974). Pico ran its
explicit runtime verifier at the first `submit_final`, recorded one `passed`
result, and then the independently injected hidden verifier passed.

| Evidence | Result |
|---|---:|
| Runtime-verification outcomes | `passed` / 1 |
| Public suite in runtime sandbox | 82 passed, 0 failed |
| Hidden verifier after Agent stopped | 1 passed, 0 failed |
| Agent attempts / tool steps | 24 / 23 |
| Agent wall time | 156.68s |
| Runtime-verifier time | 2.61s |

The run used `gpt-5.6-luna` at temperature 0 and the frozen upstream commit
`774372083b9555d41cc1c56cc1375f4011cc0054`. The reviewed summary is
[tracked JSON](../../artifacts/runtime-verification-real-oss-smoke-pytest13974.json).

## Protocol

- The Git-tracked Pico worktree was clean before the model request.
- The upstream fixture was copied into an ignored evaluation workspace; the
  frozen source fixture itself was not modified.
- Pico was given an explicit public runtime command:

  ```bash
  PYTHONPATH=src python -m pytest -q -p pytester testing/python/collect.py
  ```

  It ran inside `pico-real-oss-v1:latest` with networking disabled, a read-only
  root filesystem, 4 CPU, 4 GB memory, and a 512-PID limit.
- Only after Pico stopped was the hidden test injected and run with the frozen
  verifier command. The hidden test passed.

The runtime trace contains `runtime_verification_finished` followed by
`runtime_finalized`; the captured sandbox output reports `exit_code=0` and
`82 passed`.

## Scope and limitation

This is `pass@1 = 1/1` for a single smoke task, not a benchmark score or an
estimate of general coding ability. The public runtime suite also passes on the
unmodified fixture, so it verifies regression safety and runtime enforcement;
the post-run hidden verifier supplies issue correctness. The live repair branch
was not exercised here because the first runtime verification passed (the
one-repair state machine is covered by deterministic unit tests).

The model left `_tmp_test.py` in its evaluation workspace. Hidden correctness
passed, but this is a real output-hygiene defect; future benchmark rows should
record unexpected files or add a dedicated cleanup check rather than treating
hidden-test success as sufficient quality evidence.
