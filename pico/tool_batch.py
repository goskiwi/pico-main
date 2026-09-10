"""Parallel read runners; all Session and artifact writes stay on the caller thread."""

from concurrent.futures import ThreadPoolExecutor

from .contracts import ToolExecutionPlan, ToolFailureError
from .execution import ExecutionCancelled, ExecutionDeadlineExceeded
from .tool_context import ToolContext

READ_TOOLS = {"read_file", "list_files", "search", "read_artifact"}


def execute_group(runtime, calls, entry, execution, surface):
    outcomes = []
    cursor = 0
    while cursor < len(calls):
        execution.check_active()
        call = calls[cursor]
        if call.name not in READ_TOOLS or runtime.runtime.config.max_parallel_tools == 1:
            outcomes.append(runtime._execute(call, entry, execution, surface))
            cursor += 1
            continue
        end = cursor
        while end < len(calls) and calls[end].name in READ_TOOLS:
            end += 1
        outcomes.extend(_read_group(runtime, calls[cursor:end], entry, execution, surface))
        cursor = end
    return tuple(outcomes)


def _read_group(tools, calls, entry, execution, surface):
    submitted = []
    with ThreadPoolExecutor(max_workers=tools.runtime.config.max_parallel_tools) as pool:
        try:
            for call in calls:
                execution.check_active()
                try:
                    if call.name not in tools.resolve_surface().names:
                        raise ToolFailureError("permission_denied", "read tool is unavailable")
                    tool = surface.registry[call.name]
                    args = tool["args_schema"].model_validate(call.args).model_dump()
                    context = ToolContext(run_id=tools.runtime.session.id,
                                          tool_call_id=call.call_id,
                                          execution_context=execution.child(),
                                          execution_plan=ToolExecutionPlan("none"))
                    tool["validate"](context, args)
                except (ValueError, OSError, ToolFailureError) as exc:
                    failure = getattr(exc, "failure", None)
                    outcome = tools._finish_rejected(
                        entry, call, failure.code if failure else "invalid_arguments", str(exc),
                        structured=getattr(exc, "structured", None),
                    )
                    submitted.append((call, outcome, None))
                    continue
                tools.runtime.session.start_tool(entry, call.call_id)
                tools.runtime.session.save()
                submitted.append((call, None, pool.submit(tool["run"], context, args)))
            for call, outcome, future in submitted:
                if future is not None:
                    try:
                        result = future.result()
                        outcome = tools._outcome(call, ToolExecutionPlan("none"), result)
                    except (ExecutionCancelled, ExecutionDeadlineExceeded):
                        raise
                    except Exception as exc:  # noqa: BLE001 - read failures have no effects
                        outcome = tools._error_outcome(call, ToolExecutionPlan("none"),
                                                       "read_failed", str(exc))
                    outcome = tools._finish(entry, call, outcome)
                yield outcome
        except BaseException:
            execution.request_stop("read_group_stopped")
            for _, _, future in submitted:
                if future is not None:
                    future.cancel()
            raise
