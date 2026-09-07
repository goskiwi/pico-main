"""Optional diagnostic checks supplied by a trusted isolated execution backend."""

from typing import Literal

from pydantic import Field

from .contracts import FailureInfo, ToolRunnerResult
from .tools import ToolArgs, history_projection


class RunCheckArgs(ToolArgs):
    code: str = Field(min_length=1, max_length=16000,
                      description="Self-contained Python code; for pytest, define test functions and fixtures.")
    kind: Literal["python", "pytest"] = "python"
    timeout_seconds: int = Field(default=30, ge=1, le=60)


_BASE_RUN_CHECK_HISTORY_PROJECTION = history_projection(
    arg_fields=("kind", "timeout_seconds"),
    result_fields=(
        "purpose",
        "code_preview",
        "kind",
        "exit_code",
        "stop_reason",
        "output_limited",
    ),
)


def _run_check_history_projection(args, outcome):
    return _BASE_RUN_CHECK_HISTORY_PROJECTION(args, outcome)


HISTORY_PROJECTORS = {"run_check": _run_check_history_projection}


def _validate(context, args):
    if context.check_runner is None:
        raise ValueError("isolated check runner is unavailable")
    return args


def _run(context, args):
    result = context.check_runner(
        code=args["code"], kind=args["kind"], timeout_seconds=args["timeout_seconds"],
        execution_context=context.execution_context,
    )
    failure = None
    if result.infrastructure_error:
        failure = FailureInfo("check_infrastructure_error", result.stderr or result.stdout,
                              "retry_after_wait")
    elif result.stop_reason:
        failure = FailureInfo("check_interrupted", result.stop_reason, "retry_after_change")
    elif result.output_limited:
        failure = FailureInfo("check_output_limit", "check output exceeded its limit",
                              "retry_after_change")
    elif result.returncode != 0:
        failure = FailureInfo("check_failed", f"check exited with {result.returncode}",
                              "retry_after_change")
    return ToolRunnerResult(
        "\n".join(filter(None, [result.stdout, result.stderr])) or "Check produced no output.",
        structured={"purpose": "Isolated diagnostic requested by the model",
                    "code_preview": str(args["code"])[:1000],
                    "kind": args["kind"], "exit_code": result.returncode,
                    "stop_reason": result.stop_reason, "output_limited": result.output_limited},
        failure=failure,
    )


def build_tool_registry(*, available=True):
    return {"run_check": {
        "args_schema": RunCheckArgs,
        "risky": False,
        "available": bool(available),
        "description": (
            "Run a small Python or pytest diagnostic against current code in a fresh isolated "
            "container. Use it to test hypotheses and edge cases before submit_final, including "
            "whether state changes affect earlier callers. Choose pytest for tests using fixtures. "
            "Host workspace and official tests are not modified; no host credentials or external "
            "network are available. The timeout includes setup and cannot exceed the Run deadline. "
            "Call this tool alone. A passing diagnostic does not replace the fixed Runtime verifier "
            "or prove the task is complete."
        ),
        "validate": _validate,
        "run": _run,
        "history_projection": HISTORY_PROJECTORS["run_check"],
    }}
