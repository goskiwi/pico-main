# Pico five-repository Real OSS suite

- Overall: **5/5**
- Model: `gpt-5.6-luna`
- Runtime commit: `61207f4664005e69e621a75d213f00c70b4880db`

| Task | Result | Suite attempt | Tool steps | Duration (s) | Changed files |
|---|---:|---:|---:|---:|---|
| `click_empty_bytes_echo` | PASS | 1 | 13 | 141.0 | src/click/utils.py |
| `packaging_non_string_version` | PASS | 1 | 22 | 238.8 | src/packaging/specifiers.py, src/packaging/version.py |
| `werkzeug_float_url_notation` | PASS | 1 | 14 | 216.0 | src/werkzeug/routing/converters.py |
| `jinja_overlay_async_default` | PASS | 1 | 11 | 90.9 | src/jinja2/environment.py |
| `urllib3_port_zero` | PASS | 1 | 23 | 224.2 | src/urllib3/connectionpool.py, src/urllib3/poolmanager.py, src/urllib3/util/url.py |

Each task starts from an exact pre-fix upstream commit. Hidden verifiers are injected only after the Agent stops. This fixed five-task run is reproducibility evidence, not a general coding-success estimate.
