"""Readable validation of persisted Session sections."""

from .contracts import ToolOutcome
from .session import new_loop_control, new_verification
from .workspace import normalize_relative_file


def validate_session(value, *, schema_version):
    if not isinstance(value, dict):
        raise TypeError("session must be an object")
    if value.get("schema_version") != schema_version:
        raise ValueError("unsupported session schema")
    expected = {
        "schema_version",
        "id",
        "workspace_root",
        "history",
        "summary",
        "covered",
        "observed",
        "request_start",
        "run",
        "loop_control",
        "verification_required",
        "verification",
        "unconfirmed",
        "mutations",
        "file_states",
        "task_policy",
        "created_at",
    }
    if set(value) != expected:
        raise ValueError("invalid session fields")
    history = value["history"]
    if not isinstance(history, list):
        raise TypeError("session history must be a list")
    for key in ("covered", "observed", "request_start"):
        if type(value[key]) is not int:
            raise TypeError("history positions must be integers")
    if not 0 <= value["covered"] <= value["observed"] <= len(history):
        raise ValueError("invalid history coverage")
    if history and not 0 <= value["request_start"] < len(history):
        raise ValueError("invalid current request position")
    if history and history[value["request_start"]].get("kind") != "user":
        raise ValueError("current request must identify a user message")
    _validate_history(history)
    _validate_control(value)
    _validate_effects(value)


def _validate_history(history):
    for entry in history:
        if not isinstance(entry, dict):
            raise TypeError("history entry must be an object")
        kind = entry.get("kind")
        if kind in {"user", "feedback", "assistant"}:
            if set(entry) != {"kind", "content"} or not isinstance(entry["content"], str):
                raise ValueError("invalid message history entry")
            continue
        if kind != "tool_turn":
            raise ValueError("unknown history entry kind")
        if not {"kind", "calls", "phases", "results"} <= set(entry) <= {
            "kind", "calls", "phases", "results", "plans"
        }:
            raise ValueError("invalid tool turn fields")
        calls = entry["calls"]
        ids = set()
        for call in calls:
            if (
                not isinstance(call, dict)
                or set(call) != {"name", "args", "call_id"}
                or not isinstance(call["name"], str)
                or not isinstance(call["args"], dict)
                or not isinstance(call["call_id"], str)
            ):
                raise ValueError("invalid persisted tool call")
            ids.add(call["call_id"])
        if len(ids) != len(calls) or set(entry["phases"]) != ids:
            raise ValueError("tool phase ids do not match calls")
        if set(entry["results"]) - ids:
            raise ValueError("tool result does not match a call")
        for call_id, phase in entry["phases"].items():
            if phase not in {"pending", "running", "finished"}:
                raise ValueError("invalid tool phase")
            if (phase == "finished") != (call_id in entry["results"]):
                raise ValueError("finished phase and tool result disagree")
        _validate_results(calls, entry["results"])


def _validate_results(calls, results):
    by_id = {call["call_id"]: call for call in calls}
    for call_id, result in results.items():
        outcome = ToolOutcome.from_dict(result)
        if outcome.tool_call_id != call_id or outcome.tool_name != by_id[call_id]["name"]:
            raise ValueError("tool result identity does not match its call")


def _validate_control(value):
    if set(value["loop_control"]) != set(new_loop_control()):
        raise ValueError("invalid loop control")
    verification = value["verification"]
    if set(verification) != set(new_verification()):
        raise ValueError("invalid verification state")
    if verification["status"] not in {
        "not_run", "running", "passed", "failed", "interrupted", "stale"
    }:
        raise ValueError("invalid verification status")
    if not isinstance(value["verification_required"], bool):
        raise TypeError("verification_required must be boolean")
    policy = value["task_policy"]
    if policy and set(policy) != {"write_paths", "verification_floor"}:
        raise ValueError("invalid task policy")
    if policy:
        if not isinstance(policy["verification_floor"], bool):
            raise TypeError("verification floor must be boolean")
        paths = policy["write_paths"]
        if paths is not None:
            if not isinstance(paths, list):
                raise TypeError("write paths must be a list or null")
            for path in paths:
                normalize_relative_file(path)


def _validate_effects(value):
    for item in value["unconfirmed"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "tool", "paths", "observed"}
            or not isinstance(item["paths"], list)
            or not isinstance(item["observed"], bool)
        ):
            raise ValueError("invalid unconfirmed effect")
    receipt_fields = {
        "id", "tool", "path", "before_revision", "after_revision", "preimage_id", "status"
    }
    for receipt in value["mutations"]:
        if not isinstance(receipt, dict) or set(receipt) != receipt_fields:
            raise ValueError("invalid mutation receipt")
        if receipt["status"] not in {"prepared", "applied", "not_applied", "unknown"}:
            raise ValueError("invalid mutation receipt status")
        if normalize_relative_file(receipt["path"]) != receipt["path"]:
            raise ValueError("invalid mutation path")
    if not isinstance(value["file_states"], dict):
        raise TypeError("file states must be a dictionary")
    for path, revision in value["file_states"].items():
        normalize_relative_file(path)
        if not isinstance(revision, str):
            raise TypeError("file revision must be text")
