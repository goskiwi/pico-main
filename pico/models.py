"""LangChain-backed OpenAI-compatible model client."""

from __future__ import annotations

import json

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langsmith import tracing_context

from .actions import ModelAction
from .config import DEFAULT_MODEL_MAX_RETRIES
from .context_types import count_tokens, tokenizer_details


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _message_text(message):
    """Return text from either Responses-v1 or Chat Completions messages."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "output_text"} and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _message_preview(message, limit=800):
    text = _message_text(message)
    if text:
        return text[:limit]
    return json.dumps(getattr(message, "content", []), ensure_ascii=False)[:limit]


def _completion_metadata(message, *, cache_supported, cache_key, cache_retention):
    usage = dict(getattr(message, "usage_metadata", {}) or {})
    input_details = dict(usage.get("input_token_details", {}) or {})
    cached_tokens = sum(
        int(input_details.get(key) or 0)
        for key in ("cache_read", "priority_cache_read", "flex_cache_read", "cached_tokens")
    )
    return {
        "prompt_cache_supported": bool(cache_supported),
        "prompt_cache_key": cache_key,
        "prompt_cache_retention": cache_retention,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


def _remove_placeholder_authorization(request):
    request.headers.pop("authorization", None)


class OpenAICompatibleModelClient:
    """Thin Pico adapter around LangChain's Responses API implementation.

    Pico keeps this stable interface so the runtime, delegates, benchmarks, and
    tests do not depend on LangChain message types.  LangChain owns transport,
    retry, SSE parsing, Responses item conversion, and encrypted reasoning replay.
    """

    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        *,
        reasoning_effort=None,
        http_client=None,
    ):
        self.model = str(model)
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = str(api_key or "")
        self.temperature = temperature
        self.timeout = timeout
        self.reasoning_effort = str(reasoning_effort).strip() if reasoning_effort else None
        self.supports_prompt_cache = any(
            host in self.base_url for host in ("openai.com", "right.codes")
        )
        self.last_completion_metadata = {}

        # The OpenAI SDK requires a non-empty key even for local compatible
        # endpoints.  Preserve Pico's previous no-Authorization behavior by
        # stripping the internal placeholder before a request is sent.
        self._owned_http_client = None
        if http_client is None:
            hooks = {}
            if not self.api_key:
                hooks["request"] = [_remove_placeholder_authorization]
            self._owned_http_client = httpx.Client(event_hooks=hooks)
            http_client = self._owned_http_client
        else:
            if not self.api_key and _remove_placeholder_authorization not in http_client.event_hooks[
                "request"
            ]:
                http_client.event_hooks["request"].append(_remove_placeholder_authorization)
        self._http_client = http_client

        client_args = {
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key or "pico-no-api-key",
            "temperature": self.temperature,
            "timeout": self.timeout,
            "max_retries": DEFAULT_MODEL_MAX_RETRIES,
            "use_responses_api": True,
            # One Pico model turn is consumed as one auditable result.
            "streaming": False,
            "output_version": "responses/v1",
            "include": ["reasoning.encrypted_content"],
            "store": False,
            # Avoid LangChain installing an implicit custom transport.  Pico
            # either supplies one explicitly or lets the OpenAI SDK own it.
            "http_socket_options": (),
        }
        if self.reasoning_effort:
            client_args["reasoning"] = {"effort": self.reasoning_effort}
        client_args["http_client"] = http_client
        self._model = ChatOpenAI(**client_args)
        self.reset_action_session()

    def reset_action_session(self):
        self._action_messages = []
        self._action_pending_call_ids = []
        self._action_primary_call_id = ""
        self._action_defer_extra_calls = False
        self._action_pending_output = None

    def count_tokens(self, text):
        """Count prompt tokens with the encoding mapped to this model."""
        return count_tokens(text, model=self.model)

    def tokenizer_metadata(self):
        return tokenizer_details(self.model)

    def action_tools(self, tool_registry):
        """Return this transport's native function-definition shape."""
        from .tools import responses_action_tools

        return responses_action_tools(tool_registry)

    def fork_for_delegate(self):
        """Create an independent Responses conversation for a child agent."""
        return OpenAICompatibleModelClient(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout=self.timeout,
            reasoning_effort=self.reasoning_effort,
            http_client=self._http_client,
        )

    def close(self):
        """Close transport resources owned by this client."""
        if self._owned_http_client is not None:
            self._owned_http_client.close()
            self._owned_http_client = None

    def record_action_result(self, action, result):
        """Queue a tool or guard result for the pending Responses function call."""
        if not self._action_pending_call_ids:
            return
        if action.call_id and action.call_id not in self._action_pending_call_ids:
            raise RuntimeError("action call_id does not match the pending Responses call")
        self._action_pending_output = str(result)

    def _invoke(self, messages, max_new_tokens, *, tools=None, cache_key=None, cache_retention=None):
        kwargs = {"max_completion_tokens": int(max_new_tokens)}
        if self.supports_prompt_cache and cache_key:
            kwargs["prompt_cache_key"] = cache_key
        if self.supports_prompt_cache and cache_retention:
            kwargs["prompt_cache_retention"] = cache_retention

        runnable = self._model
        if tools is not None:
            runnable = runnable.bind_tools(
                list(tools),
                tool_choice="required",
                strict=True,
                parallel_tool_calls=False,
            )
        with tracing_context(enabled=False):
            message = runnable.invoke(list(messages), **kwargs)
        self.last_completion_metadata = _completion_metadata(
            message,
            cache_supported=self.supports_prompt_cache,
            cache_key=cache_key,
            cache_retention=cache_retention,
        )
        return message

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        message = self._invoke(
            [HumanMessage(content=str(prompt))],
            max_new_tokens,
            cache_key=prompt_cache_key,
            cache_retention=prompt_cache_retention,
        )
        text = _message_text(message)
        if text:
            return text
        raise RuntimeError("OpenAI-compatible error: could not extract text from response")

    def _append_pending_outputs(self):
        if not self._action_pending_call_ids:
            return
        if self._action_pending_output is None:
            raise RuntimeError("pending Responses function call has no recorded output")
        for call_id in self._action_pending_call_ids:
            output = self._action_pending_output
            if self._action_defer_extra_calls and call_id != self._action_primary_call_id:
                output = (
                    "deferred_by_runtime: only the first function call is executed; "
                    "call this function again if it is still needed"
                )
            self._action_messages.append(
                ToolMessage(content=output, tool_call_id=call_id)
            )
        self._action_pending_call_ids = []
        self._action_pending_output = None

    def complete_action(
        self,
        prompt,
        max_new_tokens,
        *,
        action_tools,
        prompt_cache_key=None,
        prompt_cache_retention=None,
    ):
        """Request one strict function call and normalize it to ``ModelAction``."""
        if not self._action_messages:
            self._action_messages = [HumanMessage(content=str(prompt))]
        self._append_pending_outputs()

        message = self._invoke(
            self._action_messages,
            max_new_tokens,
            tools=action_tools,
            cache_key=prompt_cache_key,
            cache_retention=prompt_cache_retention,
        )
        self._action_messages.append(message)
        action = self._action_from_message(message, action_tools)

        valid_calls = list(getattr(message, "tool_calls", []) or [])
        invalid_calls = list(getattr(message, "invalid_tool_calls", []) or [])
        all_calls = [*valid_calls, *invalid_calls]
        self._action_pending_call_ids = [
            str(call.get("id", "")).strip()
            for call in all_calls
            if str(call.get("id", "")).strip()
        ]
        self._action_primary_call_id = action.call_id
        self._action_defer_extra_calls = len(all_calls) > 1 and action.kind == "tool"
        self.last_completion_metadata.update(
            {
                "action_protocol": action.protocol,
                "structured_action": True,
                "action_kind": action.kind,
                "deferred_function_calls": (
                    len(all_calls) - 1 if self._action_defer_extra_calls else 0
                ),
            }
        )
        return action

    @staticmethod
    def _action_from_message(message, action_tools, *, protocol="responses_function"):
        calls = list(getattr(message, "tool_calls", []) or [])
        invalid_calls = list(getattr(message, "invalid_tool_calls", []) or [])
        raw_preview = _message_preview(message)
        if not calls:
            if invalid_calls:
                invalid = invalid_calls[0]
                return ModelAction.retry(
                    f"function {invalid.get('name') or '<missing>'} returned malformed JSON arguments",
                    protocol=protocol,
                    raw_preview=raw_preview,
                    call_id=str(invalid.get("id", "") or ""),
                )
            return ModelAction.retry(
                "expected exactly one function call, received 0",
                protocol=protocol,
                raw_preview=raw_preview,
            )

        allowed_names = {
            str(item.get("name") or (item.get("function") or {}).get("name") or "")
            for item in action_tools
        }
        if len(calls) + len(invalid_calls) > 1:
            names = [str(call.get("name", "")).strip() for call in [*calls, *invalid_calls]]
            if "submit_final" in names or any(name not in allowed_names for name in names):
                return ModelAction.retry(
                    "multiple function calls may contain only known non-final tools",
                    protocol=protocol,
                    raw_preview=raw_preview,
                    call_id=str(calls[0].get("id", "") or ""),
                )

        call = calls[0]
        name = str(call.get("name", "")).strip()
        call_id = str(call.get("id", "") or "").strip()
        args = call.get("args", {})
        if not isinstance(args, dict):
            return ModelAction.retry(
                f"function {name or '<missing>'} arguments must be an object",
                protocol=protocol,
                raw_preview=raw_preview,
                call_id=call_id,
            )
        if name == "submit_final":
            answer = args.get("answer")
            if set(args) != {"answer"} or not isinstance(answer, str) or not answer.strip():
                return ModelAction.retry(
                    "submit_final requires one non-empty string answer",
                    protocol=protocol,
                    raw_preview=raw_preview,
                    call_id=call_id,
                )
            return ModelAction.final(
                answer,
                protocol=protocol,
                raw_preview=raw_preview,
                call_id=call_id,
            )
        if name not in allowed_names:
            return ModelAction.retry(
                f"unknown function call: {name or '<missing>'}",
                protocol=protocol,
                raw_preview=raw_preview,
                call_id=call_id,
            )
        return ModelAction.tool(
            name,
            args,
            protocol=protocol,
            raw_preview=raw_preview,
            call_id=call_id,
        )


class DeepSeekChatCompletionsModelClient(OpenAICompatibleModelClient):
    """DeepSeek's official Chat Completions tool-calling adapter.

    Pico deliberately keeps DeepSeek in non-thinking mode. DeepSeek requires
    replaying ``reasoning_content`` across thinking-mode tool turns, while this
    adapter replays only the ordinary assistant tool-call and matching tool
    message sequence.
    """

    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        *,
        http_client=None,
    ):
        self.model = str(model)
        # DeepSeek documents the OpenAI-format base URL without a forced
        # ``/v1`` suffix. Preserve an explicitly configured gateway path.
        self.base_url = str(base_url).rstrip("/")
        self.api_key = str(api_key or "")
        self.temperature = temperature
        self.timeout = timeout
        self.reasoning_effort = None
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}
        self._owned_http_client = None
        if http_client is None:
            hooks = {}
            if not self.api_key:
                hooks["request"] = [_remove_placeholder_authorization]
            self._owned_http_client = httpx.Client(event_hooks=hooks)
            http_client = self._owned_http_client
        else:
            if not self.api_key and _remove_placeholder_authorization not in http_client.event_hooks[
                "request"
            ]:
                http_client.event_hooks["request"].append(_remove_placeholder_authorization)
        self._http_client = http_client
        self._endpoint = self.base_url + "/chat/completions"
        self.reset_action_session()

    def fork_for_delegate(self):
        return DeepSeekChatCompletionsModelClient(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout=self.timeout,
            http_client=self._http_client,
        )

    def action_tools(self, tool_registry):
        from .tools import chat_completions_action_tools

        return chat_completions_action_tools(tool_registry)

    def reset_action_session(self):
        self._action_messages = []
        self._action_pending_call_ids = []
        self._action_primary_call_id = ""
        self._action_defer_extra_calls = False
        self._action_pending_output = None

    def _post(self, payload):
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        last_error = None
        for attempt in range(DEFAULT_MODEL_MAX_RETRIES + 1):
            try:
                response = self._http_client.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeError("DeepSeek API returned a non-object response")
                return data
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
                last_error = exc
                response = getattr(exc, "response", None)
                retryable = response is None or int(response.status_code) >= 500
                if not retryable or attempt >= DEFAULT_MODEL_MAX_RETRIES:
                    break
        raise RuntimeError(f"DeepSeek Chat Completions request failed: {last_error}") from last_error

    def _invoke(self, max_new_tokens, *, tools=None):
        payload = {
            "model": self.model,
            "messages": list(self._action_messages),
            "max_tokens": int(max_new_tokens),
            "temperature": self.temperature,
            "thinking": {"type": "disabled"},
        }
        if tools is not None:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "required"
        data = self._post(payload)
        usage = dict(data.get("usage") or {})
        cached_tokens = int(usage.get("prompt_cache_hit_tokens") or 0)
        self.last_completion_metadata = {
            "prompt_cache_supported": False,
            "prompt_cache_key": None,
            "prompt_cache_retention": None,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cached_tokens": cached_tokens,
            "cache_hit": cached_tokens > 0,
        }
        choices = list(data.get("choices") or [])
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("DeepSeek API returned no completion choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError("DeepSeek API returned no assistant message")
        return message

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        del prompt_cache_key, prompt_cache_retention
        self._action_messages = [{"role": "user", "content": str(prompt)}]
        message = self._invoke(max_new_tokens)
        text = str(message.get("content") or "")
        if text:
            return text
        raise RuntimeError("DeepSeek API returned an empty text response")

    def _append_pending_outputs(self):
        if not self._action_pending_call_ids:
            return
        if self._action_pending_output is None:
            raise RuntimeError("pending DeepSeek function call has no recorded output")
        for call_id in self._action_pending_call_ids:
            output = self._action_pending_output
            if self._action_defer_extra_calls and call_id != self._action_primary_call_id:
                output = (
                    "deferred_by_runtime: only the first function call is executed; "
                    "call this function again if it is still needed"
                )
            self._action_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": output,
                }
            )
        self._action_pending_call_ids = []
        self._action_pending_output = None

    @staticmethod
    def _action_from_chat_message(message, action_tools):
        valid_calls = []
        invalid_calls = []
        for raw_call in message.get("tool_calls") or []:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function") or {}
            name = str(function.get("name", "")).strip()
            call_id = str(raw_call.get("id", "")).strip()
            raw_arguments = function.get("arguments", "{}")
            try:
                args = json.loads(raw_arguments)
            except (TypeError, json.JSONDecodeError):
                invalid_calls.append(
                    {
                        "name": name,
                        "args": raw_arguments,
                        "id": call_id,
                        "error": "invalid JSON arguments",
                    }
                )
                continue
            valid_calls.append({"name": name, "args": args, "id": call_id})
        parsed = AIMessage(
            content=str(message.get("content") or ""),
            tool_calls=valid_calls,
            invalid_tool_calls=invalid_calls,
        )
        return OpenAICompatibleModelClient._action_from_message(
            parsed,
            action_tools,
            protocol="deepseek_chat_function",
        )

    def complete_action(
        self,
        prompt,
        max_new_tokens,
        *,
        action_tools,
        prompt_cache_key=None,
        prompt_cache_retention=None,
    ):
        if not self._action_messages:
            self._action_messages = [{"role": "user", "content": str(prompt)}]
        self._append_pending_outputs()

        del prompt_cache_key, prompt_cache_retention
        message = self._invoke(max_new_tokens, tools=action_tools)
        raw_calls = list(message.get("tool_calls") or [])
        self._action_messages.append(
            {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": raw_calls,
            }
        )
        action = self._action_from_chat_message(message, action_tools)

        all_calls = [call for call in raw_calls if isinstance(call, dict)]
        self._action_pending_call_ids = [
            str(call.get("id", "")).strip()
            for call in all_calls
            if str(call.get("id", "")).strip()
        ]
        self._action_primary_call_id = action.call_id
        self._action_defer_extra_calls = len(all_calls) > 1 and action.kind == "tool"
        self.last_completion_metadata.update(
            {
                "action_protocol": action.protocol,
                "structured_action": True,
                "action_kind": action.kind,
                "deferred_function_calls": (
                    len(all_calls) - 1 if self._action_defer_extra_calls else 0
                ),
                "thinking_mode": "disabled",
            }
        )
        return action
