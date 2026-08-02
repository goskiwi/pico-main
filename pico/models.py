"""Narrow Responses client used by Pico's GPT-5.6 Luna runtime."""

from __future__ import annotations

import json

import httpx
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langsmith import tracing_context

from .agent.actions import ModelAction
from .config import DEFAULT_MODEL_MAX_RETRIES
from .context_types import count_tokens, tokenizer_details


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _message_text(message):
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"text", "output_text"}
        and isinstance(block.get("text"), str)
    )


def _message_preview(message, limit=800):
    return (_message_text(message) or json.dumps(
        getattr(message, "content", []), ensure_ascii=False
    ))[:limit]


def _responses_output_hit_token_limit(message):
    """Return whether a Responses result explicitly stopped at its output cap."""
    response_metadata = dict(getattr(message, "response_metadata", {}) or {})
    incomplete_details = dict(response_metadata.get("incomplete_details") or {})
    return (
        response_metadata.get("status") == "incomplete"
        and incomplete_details.get("reason") == "max_output_tokens"
    )


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
        "response_output_truncated": _responses_output_hit_token_limit(message),
    }


def _remove_placeholder_authorization(request):
    request.headers.pop("authorization", None)


class OpenAICompatibleModelClient:
    """Isolate the strict Responses protocol without becoming a provider framework.

    Pico uses this adapter to hide LangChain message types and normalize native
    function calls to ``ModelAction``. It deliberately does not route among
    providers or translate between model APIs.
    """

    provider = "openai_compatible_responses"

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
        self._owned_http_client = None
        if http_client is None:
            hooks = {"request": [_remove_placeholder_authorization]} if not self.api_key else {}
            self._owned_http_client = httpx.Client(event_hooks=hooks)
            http_client = self._owned_http_client
        elif not self.api_key and _remove_placeholder_authorization not in http_client.event_hooks["request"]:
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
            "streaming": False,
            "output_version": "responses/v1",
            "include": ["reasoning.encrypted_content"],
            "store": False,
            "http_socket_options": (),
            "http_client": http_client,
        }
        if self.reasoning_effort:
            client_args["reasoning"] = {"effort": self.reasoning_effort}
        self._model = ChatOpenAI(**client_args)
        self.reset_action_session()

    def reset_action_session(self):
        self._action_messages = []
        self._action_pending_call_ids = []
        self._action_primary_call_id = ""
        self._action_defer_extra_calls = False
        self._action_pending_output = None

    def count_tokens(self, text):
        return count_tokens(text, model=self.model)

    def tokenizer_metadata(self):
        return tokenizer_details(self.model)

    def action_tools(self, tool_registry):
        from .tools import responses_action_tools

        return responses_action_tools(tool_registry)

    def fork_for_delegate(self):
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
        if self._owned_http_client is not None:
            self._owned_http_client.close()
            self._owned_http_client = None

    def record_action_result(self, action, result):
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
                list(tools), tool_choice="required", strict=True, parallel_tool_calls=False
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
            self._action_messages.append(ToolMessage(content=output, tool_call_id=call_id))
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
        if all_calls and _responses_output_hit_token_limit(message):
            action = ModelAction.retry(
                "Responses output reached max_output_tokens; none of its function calls were executed. "
                "Return exactly one complete function call.",
                protocol="responses_function",
                raw_preview=_message_preview(message),
                call_id=action.call_id,
            )
        self._action_pending_call_ids = [
            str(call.get("id", "")).strip() for call in all_calls if str(call.get("id", "")).strip()
        ]
        self._action_primary_call_id = action.call_id
        self._action_defer_extra_calls = len(all_calls) > 1 and action.kind == "tool"
        self.last_completion_metadata.update(
            {
                "action_protocol": action.protocol,
                "structured_action": True,
                "action_kind": action.kind,
                "deferred_function_calls": len(all_calls) - 1 if self._action_defer_extra_calls else 0,
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
            str(item["name"])
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
            return ModelAction.final(answer, protocol=protocol, raw_preview=raw_preview, call_id=call_id)
        if name not in allowed_names:
            return ModelAction.retry(
                f"unknown function call: {name or '<missing>'}",
                protocol=protocol,
                raw_preview=raw_preview,
                call_id=call_id,
            )
        return ModelAction.tool(name, args, protocol=protocol, raw_preview=raw_preview, call_id=call_id)
