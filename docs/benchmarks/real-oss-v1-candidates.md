# Real OSS V1 candidate freeze

This document records three real upstream bug reports that passed a local
preflight for use in a future Pico benchmark. It is a **candidate freeze**, not
an Agent evaluation report: no model was run against these tasks and no success
rate should be claimed from this file.

Each candidate was checked at the upstream pull request's base commit. The
official regression test was added only for the check, then the upstream source
patch was applied. A candidate is accepted only if the injected regression test
fails before the patch and passes after it.

| ID | Upstream source | Frozen base commit | Regression check | Result |
|---|---|---|---|---|
| `pydantic_slots_forwarded_generic` | [issue #13215](https://github.com/pydantic/pydantic/issues/13215) / [PR #13243](https://github.com/pydantic/pydantic/pull/13243) | `a20c0ee267150c3bb0f82bf05e0806fa65b1e70c` | `tests/test_generics.py::test_slots_forwarded_from_generic_class` | fail before / pass after |
| `pytest_indirect_parametrize` | [issue #13974](https://github.com/pytest-dev/pytest/issues/13974) / [PR #13976](https://github.com/pytest-dev/pytest/pull/13976) | `774372083b9555d41cc1c56cc1375f4011cc0054` | `testing/python/collect.py::TestFunction::test_parametrize_overrides_parametrized_fixture_with_unrelated_indirect` | fail before / pass after |
| `click_empty_bytes_echo` | [issue #3487](https://github.com/pallets/click/issues/3487) / [PR #3493](https://github.com/pallets/click/pull/3493) | `d42f15b71757de791a5781fb179fd972da9169f5` | `tests/test_utils.py::test_echo_custom_file` | fail before / pass after |

## Preflight evidence

- Pydantic's hidden regression fails with `KeyError: '__slots__'` before the
  fix. Its official source patch forwards `__slots__` while dynamically creating
  a parameterized generic model; the same test then passes.
- Pytest's hidden regression fails during collection with `duplicate
  parametrization of 'target'` before the fix. The official patch correctly
  distinguishes direct from indirect parametrization for fixture discovery, and
  the test then passes.
- Click's hidden regression raises `TypeError: a bytes-like object is required,
  not 'str'` when `click.echo(b"", BytesIO())` is called before the fix. The
  official patch preserves an empty bytes value while adding a newline, and the
  test then passes.

## Reproduce the candidate check

For each row, clone the upstream repository, check out the frozen base commit,
fetch the PR head, and derive two patches from the PR diff: the changed test
file and the changed source file. Use the upstream project's locked development
environment where available. Apply the test patch first and run the listed test;
it must fail. Then apply the source patch and rerun the same test; it must pass.

The source patch is only a preflight oracle. It must never be copied into a
runtime fixture or exposed to the Agent. The final benchmark should expose only
the original issue text and the pre-fix checkout; it should inject a standalone
hidden verifier after the Agent stops.

## Integration gate

Before these candidates enter `real_oss_v1`, each needs all of the following:

1. A small standalone hidden verifier derived from the official regression,
   with no PR URL or solution text in the agent-visible workspace.
2. A repository-specific, digest-pinned sandbox image containing the exact test
   dependencies. The default Pico image intentionally contains only pytest and
   ruff, which is insufficient for Pydantic's compiled `pydantic-core` runtime.
3. Fixture materialization from the exact base SHA, plus a post-materialization
   tree digest and a test proving the hidden verifier is absent before Agent
   execution.
4. A frozen manifest with the issue text, allowed tools, timeout, tool budget,
   verifier command, image digest, and source/fixture snapshots committed before
   the first live-model run.

Run a small smoke suite first (the three candidates once each). Only after the
runner and isolation checks are stable should the suite grow to roughly nine
tasks and be used for a `full` versus `no_repo_map` comparison.
