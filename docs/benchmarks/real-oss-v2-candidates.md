# Real OSS V2 candidate freeze

This document freezes ten historical upstream Python bug tasks from ten
repositories. Candidate preflight is separate from the recorded 1× Agent A/B:
the latter is linked in [the V2 protocol](real-oss-v2.md) and is not a general
Pico success-rate claim.

Each task was materialized at the listed pre-fix commit. Its standalone hidden
verifier failed against that fixture, then passed after applying the associated
upstream PR patch to a temporary copy under the pinned offline Docker image.
The source patch is a preflight oracle only: it is not committed to a fixture
or exposed to the Agent.

| ID | Upstream source | Pre-fix commit | Hidden behavior | Preflight |
|---|---|---|---|---|
| `pydantic_slots_forwarded_generic` | [issue #13215](https://github.com/pydantic/pydantic/issues/13215) / [PR #13243](https://github.com/pydantic/pydantic/pull/13243) | `a20c0ee267150c3bb0f82bf05e0806fa65b1e70c` | Parameterized generic preserves explicit slots | fail / pass |
| `pytest_indirect_parametrize` | [issue #13974](https://github.com/pytest-dev/pytest/issues/13974) / [PR #13976](https://github.com/pytest-dev/pytest/pull/13976) | `774372083b9555d41cc1c56cc1375f4011cc0054` | Direct fixture override does not duplicate parametrization | fail / pass |
| `click_empty_bytes_echo` | [issue #3487](https://github.com/pallets/click/issues/3487) / [PR #3493](https://github.com/pallets/click/pull/3493) | `d42f15b71757de791a5781fb179fd972da9169f5` | Empty bytes write a binary newline | fail / pass |
| `tomlkit_float_not_sequence` | [issue #562](https://github.com/python-poetry/tomlkit/issues/562) / [PR #563](https://github.com/python-poetry/tomlkit/pull/563) | `495a42ecc9119eaaeb895def0fd025a71cd1cf60` | Parsed Float does not satisfy CPython sequence protocol | fail / pass |
| `tqdm_infinite_total_format_meter` | [PR #1781](https://github.com/tqdm/tqdm/pull/1781) | `6ab24dcc5df910044f1f6e0685f95dbf9cd424f3` | `format_meter(..., inf, ...)` matches unknown-total behavior | fail / pass |
| `packaging_non_string_version` | [issue #1318](https://github.com/pypa/packaging/issues/1318) / [PR #1319](https://github.com/pypa/packaging/pull/1319) | `0a85b41e24c9b55b83f05ae696d73e8294a5b094` | Non-string versions raise `InvalidVersion`; keyed `None` is skipped | fail / pass |
| `werkzeug_float_url_notation` | [issue #3146](https://github.com/pallets/werkzeug/issues/3146) / [PR #3147](https://github.com/pallets/werkzeug/pull/3147) | `b97e13cc74d8a45dca260d4037edd7d1f5094042` | Small float URL values do not use exponent notation | fail / pass |
| `more_itertools_negative_tail` | [PR #1194](https://github.com/more-itertools/more-itertools/pull/1194) | `5d946b3590bfe92f1465c1b9b9830dd434745c84` | Negative tail size is rejected for sized and iterator inputs | fail / pass |
| `jinja_overlay_async_default` | [PR #2061](https://github.com/pallets/jinja/pull/2061) | `767b23617628419ae3709ccfb02f9602ae9fe51f` | Overlay inherits async mode unless explicitly overridden | fail / pass |
| `urllib3_port_zero` | [PR #5071](https://github.com/urllib3/urllib3/pull/5071) | `f4e4bc31f40f8c94c6a1f1685df28f97ff48c305` | Explicit port zero remains distinct from an absent port | fail / pass |

The V2 manifest intentionally uses exactly one task from each repository. The
set spans parsing/protocol behavior, API error handling, URL routing,
configuration inheritance, collection logic, and multi-file connection-pool
semantics. It also includes several compact bugs; task count is not a proxy for
difficulty. Per-task results, costs, time, modified-file counts, and failure
types must be recorded only after a real-model run.
