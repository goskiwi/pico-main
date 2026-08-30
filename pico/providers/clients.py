"""Narrow OpenAI-compatible Responses adapter used by the runtime."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import IncompleteRead, RemoteDisconnected

from ..contracts import ModelAction

OPENAI_COMPATIBLE_USER_AGENT = "pico/0.1.0"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"


class ProviderContextOverflow(RuntimeError):
    """The provider rejected an input that exceeded its context window."""


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


class FakeModelClient:
    conversation_mode = "responses-manual-replay-v1"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.instruction_prompts = []
        self.action_tool_surfaces = []
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}
        self.reset_action_session()

    def reset_action_session(self):
        self.recorded_action_results = []

    @staticmethod
    def estimate_action_tool_tokens(_action_tools, _token_counter):
        return 0

    def record_action_result(self, action, result):
        self.recorded_action_results.append((action.kind, str(result)))

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
        **kwargs,
    ):
        self.action_tool_surfaces.append(tuple(tool["name"] for tool in action_tools))
        self.instruction_prompts.append(str(instructions))
        output = self.complete(input_text, max_new_tokens, **kwargs)
        if isinstance(output, ModelAction):
            return output
        if isinstance(output, dict):
            return _action_from_response(output, action_tools)
        raise TypeError("FakeModelClient outputs must be ModelAction or Responses payloads")


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
    last_response = None
    for event in _iter_sse_events(body_text):
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
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
            terminal["status"] = event["type"].removeprefix("response.")
            return terminal
    return last_response


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 Runtime event/report 不需要关心传输细节。
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    input_details = usage.get("input_tokens_details")
    input_details = input_details if isinstance(input_details, dict) else {}
    raw_cached_tokens = input_details.get("cached_tokens")
    cached_tokens = (
        int(raw_cached_tokens)
        if isinstance(raw_cached_tokens, (int, float))
        else None
    )
    uncached_input_tokens = (
        max(0, int(input_tokens) - cached_tokens)
        if isinstance(input_tokens, (int, float)) and cached_tokens is not None
        else None
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "uncached_input_tokens": uncached_input_tokens,
    }


def _action_from_response(data, action_tools):
    if data.get("status") == "incomplete":
        details = data.get("incomplete_details") or {}
        reason = str(details.get("reason", ""))
        if reason == "max_output_tokens":
            return ModelAction.invalid(
                "The model response reached max_output_tokens before "
                "producing one complete function call. Return exactly "
                "one concise function call."
            )
    output = data.get("output")
    if not isinstance(output, list) or any(
        not isinstance(item, dict) for item in output
    ):
        return ModelAction.invalid(
            "provider returned malformed response output"
        )
    allowed = {str(item["name"]) for item in action_tools}
    calls = [
        item for item in output if item.get("type") == "function_call"
    ]
    if len(calls) != 1:
        return ModelAction.invalid(
            f"expected exactly one function call, received {len(calls)}"
        )
    call = calls[0]
    name = str(call.get("name", "")).strip()
    if name not in allowed:
        return ModelAction.invalid(
            f"unknown function call: {name or '<missing>'}"
        )
    arguments = call.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return ModelAction.invalid(
                f"function {name} returned malformed JSON arguments"
            )
    if not isinstance(arguments, dict):
        return ModelAction.invalid(
            f"function {name} arguments must be an object"
        )
    if name == "submit_final":
        answer = arguments.get("answer")
        if set(arguments) != {"answer"} or not isinstance(answer, str) or not answer.strip():
            return ModelAction.invalid(
                "submit_final requires one non-empty string answer"
            )
        return ModelAction.final(answer)
    call_id = str(call.get("call_id") or "")
    if not call_id:
        return ModelAction.invalid(
            f"function {name} is missing a call id"
        )
    return ModelAction.tool(name, arguments, call_id=call_id)


class OpenAICompatibleModelClient:
    conversation_mode = "responses-manual-replay-v1"

    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        hostname = (
            urllib.parse.urlparse(self.base_url).hostname or ""
        ).lower().rstrip(".")
        self.supports_prompt_cache = any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in ("openai.com", "right.codes")
        )
        self.backend_hostname = hostname or "unknown"
        self.last_completion_metadata = {}
        self.reset_action_session()

    def reset_action_session(self):
        self._action_input = []
        self._pending_call_id = None

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

    def record_action_result(self, action, result):
        result = str(result)
        if self._pending_call_id is not None:
            self._action_input.append(
                {
                    "type": "function_call_output",
                    "call_id": self._pending_call_id,
                    "output": result,
                }
            )
            self._pending_call_id = None
            return
        self._action_input.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": result}],
            }
        )

    def _build_payload(
        self,
        max_new_tokens,
        *,
        instructions,
        prompt_cache_key,
        action_tools,
        input_items,
    ):
        payload = {
            "model": self.model,
            "instructions": str(instructions),
            "input": list(input_items),
            "max_output_tokens": max_new_tokens,
            "stream": False,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "tools": list(action_tools),
            "tool_choice": "required",
            "parallel_tool_calls": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        return payload

    def _request_headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request_with_retry(self, payload, request_timeout):
        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._request_headers(),
            method="POST",
        )
        attempts = 3
        total_timeout = float(self.timeout)
        if request_timeout is not None:
            total_timeout = min(total_timeout, float(request_timeout))
        deadline = time.monotonic() + max(0.001, total_timeout)

        def retry_delay(attempt):
            delay = 0.5 * (attempt + 1)
            remaining = deadline - time.monotonic()
            if remaining <= delay:
                return False
            time.sleep(delay)
            return True

        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend before the request deadline.\n"
                    f"Backend host: {self.backend_hostname}\n"
                    f"Model: {self.model}"
                )
            effective_timeout = min(float(self.timeout), remaining)
            http_failure = None
            transport_failure = False
            try:
                with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                    body_text = response.read().decode("utf-8")
                    response_headers = getattr(response, "headers", {}) or {}
                    return body_text, response_headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                try:
                    error_body = exc.read()
                except (
                    urllib.error.URLError,
                    IncompleteRead,
                    RemoteDisconnected,
                    TimeoutError,
                    OSError,
                    ValueError,
                ):
                    error_body = b""
                error_headers = getattr(exc, "headers", {}) or {}
                error_content_type = error_headers.get("Content-Type", "")
                http_failure = (
                    status,
                    _body_indicates_context_overflow(
                        error_body,
                        error_content_type,
                        allow_message_fallback=(
                            status in _HTTP_CONTEXT_MESSAGE_STATUSES
                        ),
                    ),
                    status in {408, 429} or status >= 500,
                )
            except (
                urllib.error.URLError,
                IncompleteRead,
                RemoteDisconnected,
                TimeoutError,
            ):
                transport_failure = True

            if http_failure is not None:
                status, context_overflow, transient = http_failure
                if context_overflow:
                    raise ProviderContextOverflow(_CONTEXT_OVERFLOW_MESSAGE)
                if transient and attempt < attempts - 1 and retry_delay(attempt):
                    continue
                raise RuntimeError(
                    f"OpenAI-compatible request failed with HTTP {status}.\n"
                    f"Backend host: {self.backend_hostname}\n"
                    f"Model: {self.model}"
                )
            if transport_failure:
                if attempt < attempts - 1 and retry_delay(attempt):
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Backend host: {self.backend_hostname}\n"
                    f"Model: {self.model}"
                )
        raise RuntimeError("OpenAI-compatible request exhausted retries")

    @staticmethod
    def _decode_response(body_text, content_type):
        parse_failed = False
        if _is_sse_response(body_text, content_type):
            response_data = _extract_openai_response_from_sse(body_text)
            if response_data is None:
                raise RuntimeError(
                    "OpenAI-compatible error: SSE did not contain a response object"
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
            raise RuntimeError("OpenAI-compatible error: backend returned an error")
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
        prompt_cache_key=None, request_timeout=None,
    ):
        if not self._action_input:
            self._action_input.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": str(input_text)}],
                }
            )
        if self._pending_call_id is not None:
            raise RuntimeError("pending Responses function call has no recorded output")
        self.last_completion_metadata = {}
        payload = self._build_payload(
            max_new_tokens,
            instructions=instructions,
            prompt_cache_key=prompt_cache_key,
            action_tools=action_tools,
            input_items=self._action_input,
        )
        body_text, content_type = self._request_with_retry(payload, request_timeout)
        response_data = self._decode_response(body_text, content_type)
        self.last_completion_metadata = _extract_usage_cache_details(response_data)
        action = _action_from_response(response_data, action_tools)
        output = response_data["output"]
        function_calls = [
            item for item in output if item.get("type") == "function_call"
        ]
        pending_call_id = (
            str(function_calls[0].get("call_id") or "")
            if len(function_calls) == 1
            else ""
        )
        if pending_call_id:
            self._action_input.extend(output)
            self._pending_call_id = pending_call_id
        else:
            self._action_input.extend(
                item for item in output if item.get("type") != "function_call"
            )
            self._pending_call_id = None
        return action
