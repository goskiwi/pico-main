# Official upstream public regression tests

- Overall: **5/5**
- Agent assertions passed: **25**
- Docker image ID: `sha256:8245b8e39b5ba679c7629341b9416ed08d1bff3556e9009204646eebca621ba3`

| Task | Pre-fix baseline | Reference | Agent | Agent assertions |
|---|---:|---:|---:|---:|
| `click_empty_bytes_echo` | MATCH (fail) | PASS | PASS | 1 |
| `packaging_non_string_version` | MATCH (fail) | PASS | PASS | 6 |
| `werkzeug_float_url_notation` | MATCH (fail) | PASS | PASS | 1 |
| `jinja_overlay_async_default` | MATCH (pass) | PASS | PASS | 1 |
| `urllib3_port_zero` | MATCH (fail) | PASS | PASS | 16 |

The test-only patches are copied from the bound official upstream fix commits. Every test run uses a no-network, read-only Docker workspace.
