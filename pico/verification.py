"""Runtime-owned verification bound to an exact workspace fingerprint."""

import ast
import hashlib
import re
import time
import uuid
from pathlib import Path

from .sandbox import SandboxProfile, parse_command_invocation

PYTEST_COUNT_RE = re.compile(r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed)")
PYTEST_FAILED_RE = re.compile(r"^FAILED\s+([^\s]+)", re.MULTILINE)
PYTEST_COLLECTED_RE = re.compile(r"collected\s+(\d+)\s+items?", re.IGNORECASE)
PYTEST_PROGRESS_RE = re.compile(r"^([.FEsxX]+)\s+\[\s*\d+%\]$", re.MULTILINE)


def parse_verification_output(command, output, exit_code):
    command = str(command)
    output = str(output)
    verifier = "pytest" if "pytest" in command.split() else "command"
    details = {
        "verifier": verifier,
        "collected": None,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "failed_tests": [],
    }
    if verifier == "pytest":
        collected = PYTEST_COLLECTED_RE.search(output)
        details["collected"] = int(collected.group(1)) if collected else None
        counts = {}
        for match in PYTEST_COUNT_RE.finditer(output):
            kind = match.group("kind")
            kind = "errors" if kind in {"error", "errors"} else kind
            counts[kind] = max(counts.get(kind, 0), int(match.group("count")))
        details["passed"] = counts.get("passed", 0)
        details["failed"] = counts.get("failed", 0)
        details["errors"] = counts.get("errors", 0)
        details["skipped"] = counts.get("skipped", 0)
        details["failed_tests"] = sorted(set(PYTEST_FAILED_RE.findall(output)))[:50]
        progress = "".join(PYTEST_PROGRESS_RE.findall(output))
        if progress:
            progress_counts = {
                "passed": progress.count(".") + progress.count("X"),
                "failed": progress.count("F"),
                "errors": progress.count("E"),
                "skipped": progress.count("s") + progress.count("x"),
            }
            details["collected"] = details["collected"] or len(progress)
            for kind, count in progress_counts.items():
                details[kind] = max(details[kind], count)
    signature_payload = "|".join(
        (
            verifier,
            str(exit_code),
            ",".join(details["failed_tests"]),
            str(details["failed"]),
            str(details["errors"]),
        )
    )
    details["failure_signature"] = (
        "" if exit_code == 0 else hashlib.sha256(signature_payload.encode("utf-8")).hexdigest()
    )
    return details


def changed_python_syntax_issues(agent):
    issues = []
    for relative in agent.evidence_ledger.changed_paths:
        if not relative.endswith(".py"):
            continue
        path = agent.path(relative)
        if not path.exists():
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeDecodeError) as exc:
            issues.append(f"{relative}:{getattr(exc, 'lineno', 1) or 1}: {exc}")
    return issues


def discover_verification_command(root):
    root = Path(root)
    if (root / "pyproject.toml").is_file():
        return "python -m pytest -q"
    if (root / "package.json").is_file():
        return "npm test -- --runInBand"
    return ""


def verify_workspace(
    *,
    root,
    command,
    sandbox,
    timeout_seconds,
    redact_text,
    fingerprint_provider,
    execution_context=None,
):
    command = str(command or "").strip()
    if not command:
        return None
    started = time.monotonic()
    root = Path(root).resolve()
    before = fingerprint_provider()
    record = {
        "verification_id": "verify_" + uuid.uuid4().hex[:12],
        "command": command,
        "verifier": "command",
        "status": "infrastructure_error",
        "freshness": "current",
        "workspace_fingerprint": before,
        "exit_code": None,
        "duration_ms": 0,
        "output": "",
        "collected": None,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "failed_tests": [],
        "failure_signature": "",
    }
    try:
        argv, command_env = parse_command_invocation(command)
        result = sandbox.run(
            argv,
            cwd=root,
            timeout=min(120, int(timeout_seconds)),
            env=command_env,
            execution_context=execution_context,
            profile=SandboxProfile.VERIFY,
        )
        record["exit_code"] = result.returncode
        record["output"] = redact_text(
            "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        )[:4000]
        record["status"] = "passed" if result.returncode == 0 and not result.stop_reason else "failed"
        record.update(parse_verification_output(command, record["output"], result.returncode))
    except Exception as exc:  # noqa: BLE001 - verifier infrastructure errors are audit facts
        record["output"] = redact_text(f"{type(exc).__name__}: {exc}")
    after = fingerprint_provider()
    record["workspace_fingerprint"] = after
    record["duration_ms"] = int((time.monotonic() - started) * 1000)
    if before != after:
        record["status"] = "stale"
        record["freshness"] = "stale"
    return record


def run_verification(agent):
    execution_context = (
        agent.current_execution.child(owner="runtime_verifier")
        if agent.current_execution
        else None
    )
    return verify_workspace(
        root=agent.root,
        command=agent.verification_command,
        sandbox=agent.sandbox,
        timeout_seconds=agent.run_timeout_seconds,
        redact_text=agent.redact_text,
        fingerprint_provider=agent.content_workspace_fingerprint,
        execution_context=execution_context,
    )
