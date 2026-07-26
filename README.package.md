# pico

`pico` is an auditable, sandboxed local coding-agent runtime for OpenAI-compatible
Responses models and DeepSeek's Chat Completions API.

The locally built wheel and source distribution contain only the Python runtime and
the `pico` CLI. The Docker sandbox definition, tests, benchmark fixtures, evaluation
pipeline, reports, and reproducibility evidence live in the source repository and
are intentionally not duplicated in those artifacts.

For installation, sandbox setup, architecture, security boundaries, and real-model
evaluation evidence, use the
[pico source repository](https://github.com/goskiwi/pico-main). This project is not
published under the `pico` name on PyPI.
