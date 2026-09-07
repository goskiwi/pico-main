"""Narrow OpenAI-compatible Responses adapter used by the runtime."""

import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import (
    HTTPConnection,
    HTTPException,
    HTTPSConnection,
    IncompleteRead,
    RemoteDisconnected,
)
from urllib.parse import urlsplit

from ..contracts import ModelAction, ToolCall
from ..execution import ExecutionContext

OPENAI_COMPATIBLE_USER_AGENT = "pico/0.1.0"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"

class ProviderContextOverflow(RuntimeError):
    """The provider rejected an input that exceeded its context window."""


class ProviderTransportError(RuntimeError):
    """The provider request failed before a valid HTTP response was received."""


class ProviderHTTPError(RuntimeError):
    """The provider returned an HTTP or Responses API error."""

    def __init__(self, message, *, transient=False):
        self.transient = bool(transient)
        super().__init__(str(message))


_CONTEXT_OVERFLOW_CODES = {
    "context_length_exceeded",
    "context_window_exceeded",
    "context_overflow",
    "input_too_long",
    "max_context_length_exceeded",
    "maximum_context_length_exceeded",
    "prompt_too_long",
    "token_limit_exceeded",
}
_NON_CONTEXT_ERROR_CODES = {
    "authentication_error",
    "authorization_error",
    "billing_hard_limit_reached",
    "insufficient_quota",
    "invalid_api_key",
    "permission_denied",
    "rate_limit_error",
    "rate_limit_exceeded",
    "too_many_requests",
}
_GENERIC_PROVIDER_ERROR_IDENTIFIERS = {
    "bad_request",
    "bad_request_error",
    "error",
    "invalid_request",
    "invalid_request_error",
    "request_error",
}
_CONTEXT_OVERFLOW_MESSAGE_MARKERS = (
    "context window exceeded",
    "exceeded the context window",
    "exceeds the context window",
    "input is too long",
    "max context length",
    "maximum context length",
    "prompt is too long",
)
_CONTEXT_OVERFLOW_MESSAGE = (
    "OpenAI-compatible error: provider context window exceeded"
)
_HTTP_CONTEXT_MESSAGE_STATUSES = {400, 413, 414, 422}
_TRANSIENT_PROVIDER_ERROR_CODES = {
    "internal_server_error",
    "invalid_response_event_sequence",
    "overload",
    "overloaded",
    "rate_limit_error",
    "rate_limit_exceeded",
    "server_error",
    "service_unavailable",
    "temporarily_unavailable",
    "too_many_requests",
    "upstream_error",
}


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        def origin(url):
            parsed = urlsplit(url)
            return (parsed.scheme.lower(), parsed.hostname,
                    parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80))

        if origin(req.full_url) != origin(newurl):
            raise ProviderTransportError("cross-origin Provider redirect is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _shutdown_transport(transport):
    try:
        transport.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _watch_transport_cancellation(execution_context, transport, finished):
    while not finished.wait(0.02):
        if execution_context.token.requested:
            _shutdown_transport(transport)
            return


def _arm_response_transport(execution_context, transport, deadline, finished):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _shutdown_transport(transport)
        raise TimeoutError("provider request deadline exceeded")
    timer = threading.Timer(remaining, _shutdown_transport, args=(transport,))
    timer.daemon = True
    timer.start()
    watcher = threading.Thread(
        target=_watch_transport_cancellation,
        args=(execution_context, transport, finished),
        daemon=True,
    )
    watcher.start()
    return timer, watcher


@contextmanager
def _open_response(
    request, timeout, execution_context: ExecutionContext
):
    """Open one response whose socket follows both timeout and cancellation."""
    execution_context.check_active()
    deadline = time.monotonic() + timeout
    timers = []
    watchers = []
    finished = threading.Event()

    def arm(transport):
        timer, watcher = _arm_response_transport(
            execution_context, transport, deadline, finished
        )
        timers.append(timer)
        watchers.append(watcher)

    class DeadlineHTTPConnection(HTTPConnection):
        def connect(self):
            super().connect()
            arm(self.sock)

    class DeadlineHTTPSConnection(HTTPSConnection):
        def connect(self):
            super().connect()
            arm(self.sock)

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(DeadlineHTTPConnection, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(DeadlineHTTPSConnection, req, context=self._context)


    try:
        opener = urllib.request.build_opener(HTTPHandler(), HTTPSHandler(), _SameOriginRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            with exc:
                body = exc.read()
            execution_context.check_active()
            if time.monotonic() >= deadline:
                raise TimeoutError("provider request deadline exceeded") from None
            raise urllib.error.HTTPError(
                exc.url, exc.code, exc.reason, exc.headers, io.BytesIO(body)
            ) from None
        with response:
            yield response
        execution_context.check_active()
        if time.monotonic() >= deadline:
            raise TimeoutError("provider request deadline exceeded")
    finally:
        finished.set()
        for timer in timers:
            timer.cancel()
            timer.join()
        for watcher in watchers:
            watcher.join()



def _normalized_error_code(value):
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _message_indicates_context_overflow(value):
    text = str(value).lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MESSAGE_MARKERS)


def _error_payload_indicates_context_overflow(
    value,
    *,
    allow_message_fallback=True,
):
    if not isinstance(value, dict):
        return allow_message_fallback and _message_indicates_context_overflow(value)
    identifiers = [
        _normalized_error_code(value[key])
        for key in ("code", "reason", "type")
        if value.get(key) not in (None, "")
    ]
    if any(identifier in _CONTEXT_OVERFLOW_CODES for identifier in identifiers):
        return True
    if any(identifier in _NON_CONTEXT_ERROR_CODES for identifier in identifiers):
        return False
    if any(
        not identifier.isdigit()
        and identifier not in _GENERIC_PROVIDER_ERROR_IDENTIFIERS
        for identifier in identifiers
    ):
        return False
    if not allow_message_fallback:
        return False
    return any(
        _message_indicates_context_overflow(value.get(key))
        for key in ("message", "detail")
        if value.get(key) is not None
    )


def _is_sse_response(body_text, content_type):
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    return media_type == "text/event-stream" or body_text.lstrip().startswith("data:")


def _body_indicates_context_overflow(
    body,
    content_type="",
    *,
    allow_message_fallback=True,
):
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    body = str(body or "")
    if _is_sse_response(body, content_type):
        payload = _extract_openai_response_from_sse(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        return _error_payload_indicates_context_overflow(
            error,
            allow_message_fallback=allow_message_fallback,
        )
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return allow_message_fallback and _message_indicates_context_overflow(body)
    if isinstance(payload, dict):
        payload = payload.get("error", payload)
    return _error_payload_indicates_context_overflow(
        payload,
        allow_message_fallback=allow_message_fallback,
    )


def _response_error_payload(response_data):
    if response_data.get("type") == "error":
        return True, response_data
    if response_data.get("status") == "failed":
        return True, response_data.get("error")
    if "error" in response_data and response_data.get("error") is not None:
        return True, response_data.get("error")
    return False, None


def _provider_error_detail(error):
    if isinstance(error, dict):
        if isinstance(error.get("error"), dict):
            return _provider_error_detail(error["error"])
        parts = []
        for key in ("code", "type", "reason", "message", "detail"):
            value = error.get(key)
            if value not in (None, ""):
                item = f"{key}={value}"
                if item not in parts:
                    parts.append(item)
        return "; ".join(parts) or json.dumps(error, ensure_ascii=False)
    return str(error).strip() or type(error).__name__


def _error_payload_is_transient(value):
    if not isinstance(value, dict):
        return False
    if isinstance(value.get("error"), dict):
        return _error_payload_is_transient(value["error"])
    identifiers = {
        _normalized_error_code(value[key])
        for key in ("code", "reason", "type")
        if value.get(key) not in (None, "")
    }
    return bool(identifiers & _TRANSIENT_PROVIDER_ERROR_CODES)


def _http_error_detail(body):
    if not body:
        return "no response body"
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    try:
        return _provider_error_detail(json.loads(text))
    except json.JSONDecodeError:
        return text.strip() or "empty response body"


def _retry_after_seconds(headers):
    value = str((headers or {}).get("Retry-After", "")).strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


def _retry_delay(
    deadline, attempt, execution_context: ExecutionContext, headers=None
):
    delay = max(
        0.5 * (attempt + 1),
        _retry_after_seconds(headers) or 0.0,
    )
    if deadline - time.monotonic() <= delay:
        return False
    execution_context.wait(delay)
    return True


def _read_http_failure(exc):
    status = int(exc.code)
    try:
        body = exc.read()
    except (
        urllib.error.URLError,
        IncompleteRead,
        RemoteDisconnected,
        TimeoutError,
        OSError,
        ValueError,
    ):
        body = b""
    headers = getattr(exc, "headers", {}) or {}
    content_type = headers.get("Content-Type", "")
    context_overflow = _body_indicates_context_overflow(
        body,
        content_type,
        allow_message_fallback=status in _HTTP_CONTEXT_MESSAGE_STATUSES,
    )
    return status, body, headers, context_overflow


def _action_result_items(pending_call_ids, results):
    results = tuple(str(result) for result in results)
    if pending_call_ids:
        if len(results) != len(pending_call_ids):
            raise ValueError("provider continuation requires one result per call")
        return [
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            }
            for call_id, result in zip(pending_call_ids, results)
        ]
    if len(results) != 1:
        raise ValueError("provider correction requires exactly one result")
    return [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": results[0]}],
        }
    ]


def _projected_context_tokens(
    action_input,
    result_items,
    *,
    instructions,
    action_tools,
    token_counter,
    provider_input_tokens,
    replay_output_tokens,
):
    if isinstance(provider_input_tokens, int) and isinstance(
        replay_output_tokens, int
    ):
        delta = json.dumps(
            result_items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return provider_input_tokens + replay_output_tokens + token_counter(delta)
    projected = {
        "instructions": str(instructions),
        "tools": list(action_tools),
        "input": [*action_input, *result_items],
    }
    return token_counter(
        json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _replay_output_tokens(turn):
    if not turn.accepted:
        return 0
    output_tokens = turn.usage.get("output_tokens")
    return output_tokens if isinstance(output_tokens, int) else None


class FakeModelClient:
    conversation_mode = "responses-manual-replay-v1"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.instruction_prompts = []
        self.action_tool_surfaces = []
        self.last_completion_metadata = {}
        self.reset_action_session()

    def reset_action_session(self):
        self.recorded_action_results = []
        self.recorded_action_result_groups = []
        self._action_input = []
        self._pending_call_ids = ()
        self._last_replay_output_tokens = None

    @staticmethod
    def estimate_action_tool_tokens(_action_tools, _token_counter):
        return 0

    def record_action_results(self, results):
        group = tuple(str(result) for result in results)
        self.recorded_action_result_groups.append(group)
        self.recorded_action_results.extend(group)
        self._action_input.extend(self._result_items(group))
        self._pending_call_ids = ()
        self._last_replay_output_tokens = None

    def _result_items(self, results):
        return _action_result_items(self._pending_call_ids, results)

    def projected_context_tokens(
        self,
        results,
        *,
        instructions,
        action_tools,
        token_counter,
        provider_input_tokens=None,
    ):
        return _projected_context_tokens(
            self._action_input,
            self._result_items(results),
            instructions=instructions,
            action_tools=action_tools,
            token_counter=token_counter,
            provider_input_tokens=provider_input_tokens,
            replay_output_tokens=self._last_replay_output_tokens,
        )

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)

    def complete_action(
        self,
        input_text,
        max_new_tokens,
        *,
        instructions,
        action_tools,
        execution_context,
    ):
        execution_context.check_active()
        if not self._action_input:
            self._action_input.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": str(input_text)}],
                }
        )
        self.action_tool_surfaces.append(tuple(tool["name"] for tool in action_tools))
        self.instruction_prompts.append(str(instructions))
        output = self.complete(
            input_text,
            max_new_tokens,
            execution_context=execution_context,
        )
        if isinstance(output, ModelAction):
            replay_items = ()
            pending_call_ids = ()
            if output.kind == "tool":
                replay_items = tuple(
                    _function_call_replay_item(call)
                    for call in output.tool_calls
                )
                pending_call_ids = tuple(
                    call.call_id for call in output.tool_calls
                )
            turn = _ParsedProviderTurn(
                output,
                replay_items=replay_items,
                pending_call_ids=pending_call_ids,
            )
        elif isinstance(output, dict):
            turn = _parse_provider_turn(output, action_tools)
        else:
            raise TypeError("FakeModelClient outputs must be ModelAction or Responses payloads")
        self.last_completion_metadata = dict(turn.usage)
        self._last_replay_output_tokens = _replay_output_tokens(turn)
        if turn.accepted:
            self._action_input.extend(turn.replay_items)
            self._pending_call_ids = turn.pending_call_ids
        return turn.action


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _decode_sse_data(data_lines):
    payload = "\n".join(data_lines).strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _iter_sse_events(body_text):
    data_lines = []
    for line in body_text.splitlines():
        if not line.strip():
            event = _decode_sse_data(data_lines)
            data_lines = []
            if event is not None:
                yield event
            continue
        if line == "data":
            data_lines.append("")
        elif line.startswith("data:"):
            value = line[len("data:") :]
            data_lines.append(value.removeprefix(" "))
    event = _decode_sse_data(data_lines)
    if event is not None:
        yield event


def _extract_openai_response_from_sse(body_text):
    for event in _iter_sse_events(body_text):
        response = event.get("response")
        if event.get("error"):
            return {"error": event["error"]}
        if event.get("type") == "error":
            return {
                "error": {
                    key: event[key]
                    for key in ("code", "reason", "message", "detail", "param")
                    if key in event
                }
                or True
            }
        if event.get("type") in {
            "response.completed",
            "response.failed",
            "response.incomplete",
        } and isinstance(response, dict):
            terminal = dict(response)
            status = event["type"].removeprefix("response.")
            if terminal.get("status", status) != status:
                return {"error": {"code": "inconsistent_response_status"}}
            terminal["status"] = status
            return terminal
    return None


def _extract_usage(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 Runtime event/report 不需要关心传输细节。
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
    }


def _tool_call_from_response(call):
    name = str(call.get("name", "")).strip()
    if not name:
        return None, "function call is missing a name"
    arguments = call.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None, f"function {name} returned malformed JSON arguments"
    if not isinstance(arguments, dict):
        return None, f"function {name} arguments must be an object"
    call_id = str(call.get("call_id") or "")
    if not call_id:
        return None, f"function {name} is missing a call id"
    return ToolCall(name, arguments, call_id), ""


@dataclass(frozen=True)
class _ParsedProviderTurn:
    """One fully interpreted response and the exact items safe to replay."""

    action: ModelAction
    replay_items: tuple[dict, ...] = ()
    pending_call_ids: tuple[str, ...] = ()
    usage: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.action.kind == "invalid" and (
            self.replay_items or self.pending_call_ids
        ):
            raise ValueError("invalid provider turns cannot carry replay state")
        if self.action.kind == "tool" and self.pending_call_ids != tuple(
            call.call_id for call in self.action.tool_calls
        ):
            raise ValueError("provider turn call ids do not match its action")

    @classmethod
    def invalid(cls, message, usage=None):
        return cls(ModelAction.invalid(message), usage=dict(usage or {}))

    @property
    def accepted(self):
        return self.action.kind != "invalid"


def _reasoning_replay_item(item):
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        return None, "reasoning output is missing encrypted_content"
    replay = {"type": "reasoning", "encrypted_content": encrypted}
    if "id" in item:
        if not isinstance(item["id"], str) or not item["id"]:
            return None, "reasoning output has an invalid id"
        replay["id"] = item["id"]
    if "summary" in item:
        summary = item["summary"]
        if not isinstance(summary, list) or any(
            not isinstance(part, dict) for part in summary
        ):
            return None, "reasoning output has a malformed summary"
        replay["summary"] = summary
    return replay, ""


def _message_replay_item(item):
    if item.get("status") != "completed":
        return None, "assistant message output is not completed"
    if item.get("role") != "assistant":
        return None, "message output must have the assistant role"
    message_id = item.get("id")
    if message_id is not None and (
        not isinstance(message_id, str) or not message_id
    ):
        return None, "assistant message output has an invalid id"
    content = item.get("content")
    if not isinstance(content, list) or not content:
        return None, "assistant message output has malformed content"
    normalized = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "output_text":
            return None, "assistant message output contains unsupported content"
        text = part.get("text")
        annotations = part.get("annotations", [])
        if not isinstance(text, str) or not isinstance(annotations, list):
            return None, "assistant message output has malformed output_text"
        if annotations:
            return None, "assistant message output annotations are not supported"
        normalized.append(
            {"type": "output_text", "text": text, "annotations": []}
        )
    replay = {
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": normalized,
    }
    if message_id is not None:
        replay["id"] = message_id
    return replay, ""


def _function_call_replay_item(call):
    return {
        "type": "function_call",
        "call_id": call.call_id,
        "name": call.name,
        "arguments": json.dumps(
            call.args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _provider_turn_status_error(data):
    if data.get("status") == "incomplete":
        details = data.get("incomplete_details") or {}
        reason = str(details.get("reason", ""))
        if reason == "max_output_tokens":
            return (
                "The model response reached max_output_tokens before "
                "producing complete function calls. Return one concise "
                "action or an independent tool-call group."
            )
        return (
            "The provider returned an incomplete response. Return exactly "
            "one complete action or tool-call group."
        )
    if data.get("status") != "completed":
        return "Only a completed provider response can produce actions."
    return ""


def _parse_provider_output(output, declared):
    if not isinstance(output, list) or any(
        not isinstance(item, dict) for item in output
    ):
        return (), (), "provider returned malformed response output"
    parsed_calls = []
    replay_items = []
    for item in output:
        item_type = item.get("type")
        if item_type == "reasoning":
            replay, error = _reasoning_replay_item(item)
            if error:
                return (), (), error
            replay_items.append(replay)
            continue
        if item_type == "message":
            replay, error = _message_replay_item(item)
            if error:
                return (), (), error
            replay_items.append(replay)
            continue
        if item_type != "function_call":
            return (
                (),
                (),
                f"unsupported provider output item type: {item_type or 'missing'}",
            )
        parsed_call, error = _tool_call_from_response(item)
        if error:
            return (), (), error
        if parsed_call.name not in declared:
            return (), (), f"unknown function call: {parsed_call.name}"
        parsed_calls.append(parsed_call)
        replay_items.append(_function_call_replay_item(parsed_call))
    return tuple(replay_items), tuple(parsed_calls), ""


def _parse_provider_turn(data, action_tools):
    usage = _extract_usage(data)
    error = _provider_turn_status_error(data)
    if error:
        return _ParsedProviderTurn.invalid(error, usage)
    declared = {str(item["name"]) for item in action_tools}
    replay_items, parsed_calls, error = _parse_provider_output(
        data.get("output"), declared
    )
    if error:
        return _ParsedProviderTurn.invalid(error, usage)
    if not parsed_calls:
        return _ParsedProviderTurn.invalid(
            "expected at least one function call, received 0", usage
        )

    final_calls = [call for call in parsed_calls if call.name == "submit_final"]
    if final_calls:
        if len(parsed_calls) != 1:
            return _ParsedProviderTurn.invalid(
                "submit_final must be the only call in its model response", usage
            )
        call = final_calls[0]
        answer = call.args.get("answer")
        if (
            set(call.args) != {"answer"}
            or not isinstance(answer, str)
            or not answer.strip()
        ):
            return _ParsedProviderTurn.invalid(
                "submit_final requires one non-empty string answer", usage
            )
        action = ModelAction.final(answer)
    elif len(parsed_calls) > 1:
        try:
            action = ModelAction.tools(parsed_calls)
        except ValueError as exc:
            return _ParsedProviderTurn.invalid(str(exc), usage)
    else:
        action = ModelAction.tool(
            parsed_calls[0].name,
            parsed_calls[0].args,
            call_id=parsed_calls[0].call_id,
        )
    return _ParsedProviderTurn(
        action=action,
        replay_items=replay_items,
        pending_call_ids=tuple(call.call_id for call in parsed_calls),
        usage=usage,
    )


class OpenAICompatibleModelClient:
    conversation_mode = "responses-manual-replay-v1"

    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.last_completion_metadata = {}
        self.reset_action_session()

    def reset_action_session(self):
        self._action_input = []
        self._pending_call_ids = ()
        self._last_replay_output_tokens = None

    def new_isolated_client(self):
        return OpenAICompatibleModelClient(
            self.model,
            self.base_url,
            self.api_key,
            self.temperature,
            self.timeout,
        )

    @staticmethod
    def estimate_action_tool_tokens(action_tools, token_counter):
        serialized = json.dumps(
            list(action_tools or ()),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return int(token_counter(serialized))

    def _result_items(self, results):
        return _action_result_items(self._pending_call_ids, results)

    def record_action_results(self, results):
        self._action_input.extend(self._result_items(results))
        self._pending_call_ids = ()
        self._last_replay_output_tokens = None

    def projected_context_tokens(
        self,
        results,
        *,
        instructions,
        action_tools,
        token_counter,
        provider_input_tokens=None,
    ):
        return _projected_context_tokens(
            self._action_input,
            self._result_items(results),
            instructions=instructions,
            action_tools=action_tools,
            token_counter=token_counter,
            provider_input_tokens=provider_input_tokens,
            replay_output_tokens=self._last_replay_output_tokens,
        )

    def _build_payload(
        self,
        max_new_tokens,
        *,
        instructions,
        action_tools,
        input_items,
    ):
        declared_names = tuple(str(tool["name"]) for tool in action_tools)
        if len(set(declared_names)) != len(declared_names):
            raise ValueError("action tool names must be unique")
        payload = {
            "model": self.model,
            "instructions": str(instructions),
            "input": list(input_items),
            "max_output_tokens": max_new_tokens,
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "tools": list(action_tools),
            "tool_choice": "required",
            "parallel_tool_calls": True,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return payload

    def _request_headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request_response(
        self, payload, execution_context: ExecutionContext
    ):
        execution_context.check_active()
        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._request_headers(),
            method="POST",
        )
        attempts = 3
        total_timeout = execution_context.bounded_timeout(self.timeout)
        deadline = time.monotonic() + max(0.001, total_timeout)

        for attempt in range(attempts):
            execution_context.check_active()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cause = TimeoutError(
                    f"request deadline elapsed before attempt {attempt + 1}"
                )
                raise ProviderTransportError(
                    f"OpenAI-compatible transport failed: {cause}.\n"
                    f"Backend: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from cause
            effective_timeout = execution_context.bounded_timeout(
                min(float(self.timeout), remaining)
            )
            http_failure = None
            transport_failure = None
            response_headers = {}
            try:
                with _open_response(
                    request,
                    timeout=effective_timeout,
                    execution_context=execution_context,
                ) as response:
                    body_text = response.read().decode("utf-8")
                    response_headers = getattr(response, "headers", {}) or {}
                execution_context.check_active()
                try:
                    return self._decode_response(
                        body_text,
                        response_headers.get("Content-Type", ""),
                    )
                except ProviderHTTPError as exc:
                    if (
                        exc.transient
                        and attempt < attempts - 1
                        and _retry_delay(
                            deadline,
                            attempt,
                            execution_context,
                            response_headers,
                        )
                    ):
                        continue
                    raise
            except urllib.error.HTTPError as exc:
                http_failure = _read_http_failure(exc)
            except (
                urllib.error.URLError,
                IncompleteRead,
                RemoteDisconnected,
                TimeoutError,
                HTTPException,
                OSError,
            ) as exc:
                transport_failure = exc

            execution_context.check_active()
            if http_failure is not None:
                status, error_body, error_headers, context_overflow = http_failure
                transient = status in {408, 429} or status >= 500
                if context_overflow:
                    raise ProviderContextOverflow(_CONTEXT_OVERFLOW_MESSAGE)
                if (
                    transient
                    and attempt < attempts - 1
                    and _retry_delay(
                        deadline,
                        attempt,
                        execution_context,
                        error_headers,
                    )
                ):
                    continue
                detail = _http_error_detail(error_body)
                raise ProviderHTTPError(
                    f"OpenAI-compatible request failed with HTTP {status}: {detail}.\n"
                    f"Backend: {self.base_url}\n"
                    f"Model: {self.model}",
                    transient=transient,
                )
            if transport_failure is not None:
                if attempt < attempts - 1 and _retry_delay(
                    deadline, attempt, execution_context
                ):
                    continue
                raise ProviderTransportError(
                    "OpenAI-compatible transport failed after "
                    f"{attempt + 1} attempts: {type(transport_failure).__name__}: "
                    f"{transport_failure}.\n"
                    f"Backend: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from transport_failure
        raise RuntimeError("OpenAI-compatible request exhausted retries")

    @staticmethod
    def _decode_response(body_text, content_type):
        parse_failed = False
        if _is_sse_response(body_text, content_type):
            response_data = _extract_openai_response_from_sse(body_text)
            if response_data is None:
                raise ProviderHTTPError(
                    "OpenAI-compatible error: SSE ended without a terminal "
                    "response object",
                    transient=True,
                )
        else:
            try:
                response_data = json.loads(body_text)
            except json.JSONDecodeError:
                response_data = None
                parse_failed = True
        if parse_failed:
            raise RuntimeError(
                "OpenAI-compatible error: backend returned non-JSON "
                "content that could not be parsed"
            )
        if not isinstance(response_data, dict):
            raise RuntimeError(  # noqa: TRY004 - provider protocol failure
                "OpenAI-compatible error: backend returned a non-object JSON response"
            )
        has_error, error = _response_error_payload(response_data)
        if has_error and _error_payload_indicates_context_overflow(error):
            raise ProviderContextOverflow(_CONTEXT_OVERFLOW_MESSAGE)
        if has_error:
            raise ProviderHTTPError(
                "OpenAI-compatible response error: " + _provider_error_detail(error),
                transient=_error_payload_is_transient(error),
            )
        output = response_data.get("output")
        if not isinstance(output, list) or any(
            not isinstance(item, dict) for item in output
        ):
            raise RuntimeError(
                "OpenAI-compatible error: malformed response output"
            )
        return response_data

    def complete_action(
        self, input_text, max_new_tokens, *, instructions, action_tools,
        execution_context: ExecutionContext,
    ):
        execution_context.check_active()
        if not self._action_input:
            self._action_input.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": str(input_text)}],
                }
            )
        if self._pending_call_ids:
            raise RuntimeError("pending Responses function calls have no recorded outputs")
        self.last_completion_metadata = {}
        payload = self._build_payload(
            max_new_tokens,
            instructions=instructions,
            action_tools=action_tools,
            input_items=self._action_input,
        )
        response_data = self._request_response(payload, execution_context)
        turn = _parse_provider_turn(response_data, action_tools)
        self.last_completion_metadata = dict(turn.usage)
        self._last_replay_output_tokens = _replay_output_tokens(turn)
        if turn.accepted:
            self._action_input.extend(turn.replay_items)
            self._pending_call_ids = turn.pending_call_ids
        return turn.action
