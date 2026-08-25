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


class FakeModelClient:
    conversation_mode = "responses-manual-replay-v1"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
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

    def complete_action(self, prompt, max_new_tokens, *, action_tools, **kwargs):
        self.action_tool_surfaces.append(tuple(tool["name"] for tool in action_tools))
        output = self.complete(prompt, max_new_tokens, **kwargs)
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


def _iter_sse_events(body_text):
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _extract_openai_response_from_sse(body_text):
    last_response = None
    for event in _iter_sse_events(body_text):
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
        if event.get("type") in {
            "response.completed",
            "response.failed",
            "response.incomplete",
        } and isinstance(response, dict):
            return response
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
        self._pending_call_ids = []

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
        if self._pending_call_ids:
            self._action_input.extend(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                }
                for call_id in self._pending_call_ids
            )
            self._pending_call_ids = []
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
        prompt_cache_key,
        action_tools,
        input_items,
    ):
        payload = {
            "model": self.model,
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
            try:
                with urllib.request.urlopen(request, timeout=effective_timeout) as response:
                    body_text = response.read().decode("utf-8")
                    response_headers = getattr(response, "headers", {}) or {}
                    return body_text, response_headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                transient = exc.code in {408, 429} or exc.code >= 500
                if transient and attempt < attempts - 1 and retry_delay(attempt):
                    continue
                raise RuntimeError(
                    f"OpenAI-compatible request failed with HTTP {exc.code}.\n"
                    f"Backend host: {self.backend_hostname}\n"
                    f"Model: {self.model}"
                ) from exc
            except (
                urllib.error.URLError,
                IncompleteRead,
                RemoteDisconnected,
                TimeoutError,
            ) as exc:
                if attempt < attempts - 1 and retry_delay(attempt):
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Backend host: {self.backend_hostname}\n"
                    f"Model: {self.model}"
                ) from exc
        raise RuntimeError("OpenAI-compatible request exhausted retries")

    @staticmethod
    def _decode_response(body_text, content_type):
        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith(
            "data:"
        ):
            response_data = _extract_openai_response_from_sse(body_text)
            if response_data is None:
                raise RuntimeError(
                    "OpenAI-compatible error: SSE did not contain a response object"
                )
        else:
            try:
                response_data = json.loads(body_text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "OpenAI-compatible error: backend returned non-JSON "
                    "content that could not be parsed"
                ) from exc
        if not isinstance(response_data, dict):
            raise RuntimeError(  # noqa: TRY004 - provider protocol failure
                "OpenAI-compatible error: backend returned a non-object JSON response"
            )
        if response_data.get("error"):
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
        self, prompt, max_new_tokens, *, action_tools,
        prompt_cache_key=None, request_timeout=None,
    ):
        if not self._action_input:
            self._action_input.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": str(prompt)}],
                }
            )
        if self._pending_call_ids:
            raise RuntimeError("pending Responses function call has no recorded output")
        self.last_completion_metadata = {}
        payload = self._build_payload(
            max_new_tokens,
            prompt_cache_key=prompt_cache_key,
            action_tools=action_tools,
            input_items=self._action_input,
        )
        body_text, content_type = self._request_with_retry(payload, request_timeout)
        response_data = self._decode_response(body_text, content_type)
        self.last_completion_metadata = _extract_usage_cache_details(response_data)
        action = _action_from_response(response_data, action_tools)
        output = response_data["output"]
        self._action_input.extend(output)
        self._pending_call_ids = [
            str(item.get("call_id") or "")
            for item in output
            if item.get("type") == "function_call"
            and str(item.get("call_id") or "")
        ]
        return action
