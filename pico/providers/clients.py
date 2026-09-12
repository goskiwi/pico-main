"""Small Responses API adapter; transport and SSE parsing belong to the SDK."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    DefaultHttpxClient,
    OpenAI,
)

from ..contracts import ModelAction, ToolCall
from ..execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
CONTEXT_OVERFLOW_CODES = {
    "context_length_exceeded",
    "context_window_exceeded",
    "context_overflow",
    "input_too_long",
    "max_context_length_exceeded",
    "prompt_too_long",
    "token_limit_exceeded",
}
CONTEXT_OVERFLOW_MARKERS = (
    "context window exceeded",
    "exceeded the context window",
    "input is too long",
    "max context length",
    "prompt is too long",
)


class ProviderContextOverflow(RuntimeError):
    """The provider rejected input that exceeded its context window."""


def _context_overflow(error):
    body = getattr(error, "body", None)
    body = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(body, dict):
        values = [body.get(key) for key in ("code", "type", "reason")]
        if any(str(value or "").lower() in CONTEXT_OVERFLOW_CODES for value in values):
            return True
        text = " ".join(str(body.get(key, "")) for key in ("message", "detail"))
    else:
        text = str(body or error)
    lowered = text.lower()
    return any(marker in lowered for marker in CONTEXT_OVERFLOW_MARKERS)


def _result_items(pending_call_id, results):
    results = tuple(str(result) for result in results)
    if pending_call_id:
        if len(results) != 1:
            raise ValueError("provider continuation requires exactly one result")
        return [{
            "type": "function_call_output",
            "call_id": pending_call_id,
            "output": results[0],
        }]
    if len(results) != 1:
        raise ValueError("provider correction requires exactly one result")
    return [{
        "role": "user",
        "content": [{"type": "input_text", "text": results[0]}],
    }]


def _estimate_input(action_input, input_text, instructions, action_tools, count_tokens):
    items = action_input or [{
        "role": "user",
        "content": [{"type": "input_text", "text": str(input_text)}],
    }]
    return count_tokens(json.dumps({
        "instructions": str(instructions),
        "tools": list(action_tools),
        "input": items,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _projected_tokens(action_input, result_items, *, instructions, action_tools,
                      count_tokens, replay_context_tokens):
    if isinstance(replay_context_tokens, int):
        return replay_context_tokens + count_tokens(json.dumps(
            result_items, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
    return count_tokens(json.dumps({
        "instructions": str(instructions),
        "tools": list(action_tools),
        "input": [*action_input, *result_items],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _usage(data):
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    details = usage.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return {
        "input_tokens": usage.get("input_tokens"),
        "cached_tokens": cached if type(cached) is int and cached >= 0 else None,
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _replay_context_tokens(turn):
    input_tokens = turn.usage.get("input_tokens")
    output_tokens = turn.usage.get("output_tokens") if turn.accepted else 0
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        return input_tokens + output_tokens
    return None


def _tool_call(item):
    name = str(item.get("name", "")).strip()
    call_id = str(item.get("call_id", "")).strip()
    arguments = item.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None, f"function {name or '<missing>'} returned invalid JSON"
    if not name or not call_id or not isinstance(arguments, dict):
        return None, "function call requires a name, call_id and object arguments"
    return ToolCall(name, arguments, call_id), ""


def _function_call_item(call):
    return {
        "type": "function_call",
        "call_id": call.call_id,
        "name": call.name,
        "arguments": json.dumps(
            call.args, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ),
    }


def _replay_item(item):
    item_type = item.get("type")
    if item_type == "reasoning":
        encrypted = item.get("encrypted_content")
        if not isinstance(encrypted, str) or not encrypted:
            return None, "reasoning output is missing encrypted_content"
        replay = {"type": "reasoning", "encrypted_content": encrypted}
        for key in ("id", "summary"):
            if key in item:
                replay[key] = item[key]
        return replay, ""
    if item_type == "message":
        if item.get("status") != "completed" or item.get("role") != "assistant":
            return None, "assistant message output is not completed"
        content = item.get("content")
        if not isinstance(content, list) or any(
            not isinstance(part, dict) or part.get("type") != "output_text"
            for part in content
        ):
            return None, "assistant message output contains unsupported content"
        replay = {
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": content,
        }
        if item.get("id"):
            replay["id"] = item["id"]
        return replay, ""
    return None, f"unsupported provider output item type: {item_type or 'missing'}"


@dataclass(frozen=True)
class ParsedTurn:
    action: ModelAction
    replay_items: tuple[dict, ...] = ()
    pending_call_id: str = ""
    usage: dict = field(default_factory=dict)

    @property
    def accepted(self):
        return self.action.kind in {"tool", "final"}

    @classmethod
    def failed(cls, kind, message, usage=None):
        factory = {
            "truncated": ModelAction.truncated,
            "service_failed": ModelAction.service_failed,
            "protocol_error": ModelAction.protocol_error,
        }[kind]
        return cls(factory(message), usage=dict(usage or {}))


def _parse_turn(data, action_tools):
    usage = _usage(data)
    status = data.get("status")
    if status == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason", "")
        return ParsedTurn.failed(
            "truncated",
            f"Provider response was truncated: {reason or 'incomplete'}",
            usage,
        )
    if status == "failed":
        error = data.get("error") or {}
        detail = error.get("message") if isinstance(error, dict) else error
        return ParsedTurn.failed(
            "service_failed",
            f"Provider response failed: {detail or 'service failure'}",
            usage,
        )
    if status != "completed":
        return ParsedTurn.failed(
            "protocol_error", f"Provider returned unknown status: {status}", usage
        )
    output = data.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        return ParsedTurn.failed(
            "protocol_error", "provider returned malformed response output", usage
        )
    declared = {str(tool["name"]) for tool in action_tools}
    replay = []
    calls = []
    for item in output:
        if item.get("type") == "function_call":
            call, error = _tool_call(item)
            if error:
                return ParsedTurn.failed("protocol_error", error, usage)
            if call.name not in declared:
                return ParsedTurn.failed(
                    "protocol_error", f"unknown function call: {call.name}", usage
                )
            calls.append(call)
            replay.append(_function_call_item(call))
            continue
        normalized, error = _replay_item(item)
        if error:
            return ParsedTurn.failed("protocol_error", error, usage)
        replay.append(normalized)
    if len(calls) != 1:
        return ParsedTurn.failed(
            "protocol_error",
            "Pico requires exactly one function call per model response",
            usage,
        )
    call = calls[0]
    if call.name == "submit_final":
        answer = call.args.get("answer")
        if set(call.args) != {"answer"} or not isinstance(answer, str) or not answer.strip():
            return ParsedTurn.failed(
                "protocol_error", "submit_final requires one non-empty answer", usage
            )
        action = ModelAction.final(answer)
    else:
        action = ModelAction.tool(call.name, call.args, call_id=call.call_id)
    return ParsedTurn(action, tuple(replay), call.call_id, usage)


class OpenAICompatibleModelClient:
    """Stateful manual replay over one stateless Responses connection."""

    conversation_mode = "responses-manual-replay-v1"

    def __init__(self, model, base_url, api_key, temperature, timeout,
                 reasoning_effort=""):
        self.model = str(model)
        self.base_url = str(base_url).rstrip("/")
        self.api_key = str(api_key)
        self.temperature = temperature
        self.timeout = float(timeout)
        self.reasoning_effort = str(reasoning_effort or "").strip()
        self._client = self._new_sdk_client()
        self.last_completion_metadata = {}
        self.reset_action_session()

    def _new_sdk_client(self):
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=2,
            http_client=DefaultHttpxClient(follow_redirects=False),
        )

    def reset_action_session(self):
        self._action_input = []
        self._pending_call_id = ""
        self._replay_context_tokens = None

    def new_isolated_client(self):
        return OpenAICompatibleModelClient(
            self.model, self.base_url, self.api_key, self.temperature,
            self.timeout, self.reasoning_effort,
        )

    @staticmethod
    def estimate_action_tool_tokens(action_tools, count_tokens):
        return count_tokens(json.dumps(
            list(action_tools or ()),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))

    def estimate_action_input_tokens(self, input_text, *, instructions,
                                     action_tools, token_counter):
        return _estimate_input(
            self._action_input, input_text, instructions, action_tools, token_counter,
        )

    def _result_items(self, results):
        return _result_items(self._pending_call_id, results)

    def record_action_results(self, results):
        self._action_input.extend(self._result_items(results))
        self._pending_call_id = ""
        self._replay_context_tokens = None

    def projected_context_tokens(self, results, *, instructions, action_tools,
                                 token_counter):
        return _projected_tokens(
            self._action_input,
            self._result_items(results),
            instructions=instructions,
            action_tools=action_tools,
            count_tokens=token_counter,
            replay_context_tokens=self._replay_context_tokens,
        )

    def _payload(self, input_text, max_new_tokens, instructions, action_tools):
        if not self._action_input:
            self._action_input.append({
                "role": "user",
                "content": [{"type": "input_text", "text": str(input_text)}],
            })
        payload = {
            "model": self.model,
            "instructions": str(instructions),
            "input": list(self._action_input),
            "max_output_tokens": int(max_new_tokens),
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "tools": list(action_tools),
            "tool_choice": "required",
            "parallel_tool_calls": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        return payload

    def _request(self, payload, execution_context):
        timeout = execution_context.bounded_timeout(self.timeout)
        client = self._client.with_options(timeout=timeout)
        watcher_done = threading.Event()
        interrupted = threading.Event()

        def close_on_stop():
            while not watcher_done.wait(
                min(0.05, max(0.0, execution_context.remaining_seconds()))
            ):
                if (
                    execution_context.token.requested
                    or execution_context.remaining_seconds() <= 0
                ):
                    interrupted.set()
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001, S110 - best-effort close
                        pass
                    return

        watcher = threading.Thread(
            target=close_on_stop,
            name="pico-model-cancellation",
            daemon=True,
        )
        watcher.start()
        response = None
        try:
            stream = client.responses.create(**payload)
            with stream:
                for event in stream:
                    execution_context.check_active()
                    if event.type in {
                        "response.completed", "response.incomplete", "response.failed",
                    }:
                        response = event.response
        except APIStatusError as exc:
            execution_context.check_active()
            if _context_overflow(exc):
                raise ProviderContextOverflow(
                    "provider context window exceeded"
                ) from exc
            raise RuntimeError(
                f"Provider HTTP {exc.status_code}: {exc.message}"
            ) from exc
        except (APIConnectionError, APITimeoutError) as exc:
            execution_context.check_active()
            raise RuntimeError(f"Provider transport failed: {exc}") from exc
        except BaseException:
            execution_context.check_active()
            raise
        finally:
            watcher_done.set()
            watcher.join(timeout=0.1)
            if interrupted.is_set():
                self._client = self._new_sdk_client()
        execution_context.check_active()
        if response is None:
            raise RuntimeError("Provider stream ended without a terminal response")
        return response.model_dump(mode="json")

    def complete_action(self, input_text, max_new_tokens, *, instructions,
                        action_tools, execution_context: ExecutionContext):
        if self._pending_call_id:
            raise RuntimeError("pending function call has no recorded output")
        try:
            response = self._request(
                self._payload(input_text, max_new_tokens, instructions, action_tools),
                execution_context,
            )
        except ProviderContextOverflow:
            raise
        except (ExecutionCancelled, ExecutionDeadlineExceeded):
            raise
        except RuntimeError as exc:
            turn = ParsedTurn.failed("service_failed", str(exc))
        else:
            turn = _parse_turn(response, action_tools)
        self.last_completion_metadata = dict(turn.usage)
        self._replay_context_tokens = _replay_context_tokens(turn)
        if turn.accepted:
            self._action_input.extend(turn.replay_items)
            self._pending_call_id = turn.pending_call_id
        return turn.action
