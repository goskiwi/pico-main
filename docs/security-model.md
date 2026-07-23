# Pico security model

Pico executes model-selected actions on a local repository. Its security goal is
to make those actions explicit, bounded, reviewable, and contained relative to
the rest of the host. It is not a hardened multi-tenant sandbox and does not
claim that an approved action cannot damage the writable workspace.

## Assets and trust boundaries

| Asset | Boundary |
|---|---|
| Files outside the selected workspace | File tools resolve paths under the fixed workspace root; shell sees only the workspace bind mount plus container filesystems. |
| Repository metadata | `.git` is mounted read-only inside shell containers and protected from file-tool writes. |
| Local secrets | `.env*` files are masked in the container, sensitive environment names are redacted, and shell receives only an allowlisted environment. |
| Host network and operating system | Shell runs with Docker networking disabled, a read-only root filesystem, dropped capabilities, `no-new-privileges`, and resource limits. |
| Model credentials and repository context | The configured model endpoint is trusted to receive its API key, prompts, selected context, and tool results. Docker does not protect this remote data flow. |
| Writable workspace | The user, approval policy, and version control are the protection boundary. Coding actions are expected to modify these files. |

## Action path

Every requested action follows the common validation and audit path, with an
execution branch appropriate to the tool:

```text
strict model action
  -> local argument schema
  -> path and dangerous-command validation
  -> capability / read-only policy
  -> approval or dry-run decision
  -> file tool: host-side operation inside the fixed workspace root
     OR shell tool: filtered environment and Docker execution
  -> workspace diff
  -> trace and report audit
```

Provider-side strict function calling improves protocol reliability but does not
replace local validation or authorization.

## Model endpoint and environment handling

- The default endpoint is the official OpenAI API. Compatible or third-party
  endpoints must be configured explicitly with `OPENAI_API_BASE` or `--base-url`.
- Model settings are read from the selected workspace's `.env.local` and passed
  to the model client as an explicit mapping. Pico does not bulk-export those
  assignments into `os.environ`.
- API keys are not written to session, trace, report, or benchmark artifacts.
- LangChain and LangGraph tracing is disabled in code, even when ambient
  LangSmith tracing environment variables are set, so framework telemetry does
  not create an additional prompt or tool-output destination.
- A custom endpoint can observe repository information sent through prompts and
  tool outputs. Users must evaluate that endpoint's retention and access policy.

## Delegate boundary

Delegate children have workspace-read-only tool capabilities and cannot execute
shell commands, write files, or create nested delegates. They still create
`.pico` run and session artifacts for audit. Child sessions and runs are marked
as delegate records and excluded from default interactive resume/recent-run
selection. Read-only investigation disables both rule-based and LLM-based
durable-memory promotion.

## Docker boundary

The container receives a writable bind mount for the selected workspace because
the agent must be able to run tests that create files and, after approval, make
repository changes. The container boundary protects the rest of the host; it is
not a rollback mechanism for the workspace. Important repositories should be
under version control and workspace diffs should be reviewed before commit.

Pico assumes the local Docker daemon and configured sandbox image are trusted.
It does not defend against a compromised daemon, kernel vulnerability, malicious
image supply chain, or another host process modifying the workspace concurrently.

## Explicit non-goals

- multi-tenant identity, authorization, or quota isolation;
- remote worker hardening and centralized secret management;
- prevention of all destructive changes inside an approved writable workspace;
- formal containment of arbitrary hostile native code;
- privacy guarantees made by a remote model provider;
- automatic code review or correctness proof.

## Verification

The offline suite tests schema, path, approval, redaction, and audit behavior.
The opt-in Docker integration suite additionally exercises a real container's
network isolation, read-only root filesystem, secret masking, resource limits,
workspace write behavior, and timeout cleanup:

```bash
docker build -f Dockerfile.sandbox -t pico-sandbox:latest .
PICO_RUN_DOCKER_TESTS=1 uv run pytest -q tests/test_sandbox.py
```

Live-model tests are separate because they validate provider integration and
model behavior rather than the local containment boundary.
