"""Small Responses API adapter; transport and SSE parsing belong to the SDK."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    DefaultAsyncHttpxClient,
)

from ..contracts import AssistantTurn, ModelAction, ModelMessage, ToolCall
from ..execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
MAX_TOOL_CALLS_PER_TURN = 8
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


def _result_items(pending_call_ids, results):
    pending_call_ids = tuple(str(call_id) for call_id in pending_call_ids)
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
            for call_id, result in zip(pending_call_ids, results, strict=True)
        ]
    if len(results) != 1:
        raise ValueError("provider correction requires exactly one result")
    return [{
        "role": "user",
        "content": [{"type": "input_text", "text": results[0]}],
    }]


def _message_items(messages):
    items = []
    for message in messages:
        if not isinstance(message, ModelMessage):
            raise TypeError("model prompt messages must be ModelMessage values")
        if message.role in {"developer", "user"}:
            items.append({
                "role": message.role,
                "content": [{"type": "input_text", "text": message.text}],
            })
            continue
        if message.role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": message.text,
            })
            continue
        if message.text:
            items.append({
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": message.text}],
            })
        items.extend(_function_call_item(call) for call in message.tool_calls)
    return items


def _estimate_input(action_input, messages, system_prompt, action_tools, count_tokens):
    items = action_input or _message_items(messages)
    return count_tokens(json.dumps({
        "instructions": str(system_prompt),
        "tools": list(action_tools),
        "input": items,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _projected_tokens(action_input, result_items, *, system_prompt, action_tools,
                      count_tokens, replay_context_tokens):
    if isinstance(replay_context_tokens, int):
        return replay_context_tokens + count_tokens(json.dumps(
            result_items, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
    return count_tokens(json.dumps({
        "instructions": str(system_prompt),
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
            not isinstance(part, dict)
            or part.get("type") != "output_text"
            or not isinstance(part.get("text"), str)
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
    pending_call_ids: tuple[str, ...] = ()
    text: str = ""
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


def _action_from_calls(calls):
    if not calls:
        raise ValueError("Pico requires at least one function call per model response")
    if len(calls) > MAX_TOOL_CALLS_PER_TURN:
        raise ValueError(
            f"Pico accepts at most {MAX_TOOL_CALLS_PER_TURN} function calls per response"
        )
    if len({call.call_id for call in calls}) != len(calls):
        raise ValueError("function call ids must be unique")
    final_calls = [call for call in calls if call.name == "submit_final"]
    if final_calls:
        if len(calls) != 1:
            raise ValueError("submit_final must be the only function call")
        answer = final_calls[0].args.get("answer")
        if (
            set(final_calls[0].args) != {"answer"}
            or not isinstance(answer, str)
            or not answer.strip()
        ):
            raise ValueError("submit_final requires one non-empty answer")
        return ModelAction.final(answer), ()
    action = (
        ModelAction.tool(calls[0].name, calls[0].args, call_id=calls[0].call_id)
        if len(calls) == 1
        else ModelAction.tools(calls)
    )
    return action, tuple(call.call_id for call in calls)


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
    visible = []
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
        if item.get("type") == "message":
            visible.extend(
                part["text"]
                for part in item["content"]
                if part["text"].strip()
            )
    try:
        action, pending_call_ids = _action_from_calls(calls)
    except ValueError as exc:
        return ParsedTurn.failed("protocol_error", str(exc), usage)
    return ParsedTurn(
        action=action,
        replay_items=tuple(replay),
        pending_call_ids=pending_call_ids,
        text="\n".join(visible).strip(),
        usage=usage,
    )


class OpenAICompatibleModelClient:
    """Stateful manual replay over one session-scoped Responses transport."""

    conversation_mode = "responses-manual-replay-v1"

    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        reasoning_effort="",
        context_window_tokens=None,
    ):
        self.model = str(model)
        self.base_url = str(base_url).rstrip("/")
        self.api_key = str(api_key)
        self.temperature = temperature
        self.timeout = float(timeout)
        self.reasoning_effort = str(reasoning_effort or "").strip()
        self.context_window_tokens = (
            None if context_window_tokens is None else int(context_window_tokens)
        )
        if self.context_window_tokens is not None and self.context_window_tokens < 1:
            raise ValueError("model context window must be positive")
        self.last_completion_metadata = {}
        self._runner = asyncio.Runner()
        self._sdk_client = None
        self._closed = False
        self.reset_action_session()

    def _new_sdk_client(self):
        return AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=2,
            http_client=DefaultAsyncHttpxClient(follow_redirects=False),
        )

    def reset_action_session(self):
        self._action_input = []
        self._pending_call_ids = ()
        self._replay_context_tokens = None

    def new_isolated_client(self):
        return OpenAICompatibleModelClient(
            self.model,
            self.base_url,
            self.api_key,
            self.temperature,
            self.timeout,
            self.reasoning_effort,
            self.context_window_tokens,
        )

    @staticmethod
    def estimate_action_tool_tokens(action_tools, count_tokens):
        return count_tokens(json.dumps(
            list(action_tools or ()),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))

    def estimate_action_input_tokens(self, messages, *, system_prompt,
                                     action_tools, token_counter):
        return _estimate_input(
            self._action_input,
            messages,
            system_prompt,
            action_tools,
            token_counter,
        )

    def _result_items(self, results):
        return _result_items(self._pending_call_ids, results)

    def record_action_results(self, results):
        self._action_input.extend(self._result_items(results))
        self._pending_call_ids = ()
        self._replay_context_tokens = None

    def projected_context_tokens(self, results, *, system_prompt, action_tools,
                                 token_counter):
        return _projected_tokens(
            self._action_input,
            self._result_items(results),
            system_prompt=system_prompt,
            action_tools=action_tools,
            count_tokens=token_counter,
            replay_context_tokens=self._replay_context_tokens,
        )

    def _payload(self, messages, max_output_tokens, system_prompt, action_tools):
        if not self._action_input:
            self._action_input.extend(_message_items(messages))
        payload = {
            "model": self.model,
            "instructions": str(system_prompt),
            "input": list(self._action_input),
            "max_output_tokens": int(max_output_tokens),
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "tools": list(action_tools),
            "tool_choice": "required",
            "parallel_tool_calls": True,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        return payload

    def _request(self, payload, execution_context):
        if self._closed:
            raise RuntimeError("model client is closed")
        return self._runner.run(self._request_async(payload, execution_context))

    async def _request_async(self, payload, execution_context):
        timeout = execution_context.bounded_timeout(self.timeout)

        async def receive():
            if self._sdk_client is None:
                self._sdk_client = self._new_sdk_client()
            client = self._sdk_client.with_options(timeout=timeout)
            stream = await client.responses.create(**payload)
            response = None
            async with stream:
                async for event in stream:
                    execution_context.check_active()
                    if event.type in {
                        "response.completed", "response.incomplete", "response.failed",
                    }:
                        response = event.response
            if response is None:
                raise RuntimeError("Provider stream ended without a terminal response")
            return response.model_dump(mode="json")

        request = asyncio.create_task(receive())
        try:
            while not request.done():
                execution_context.check_active()
                await asyncio.wait(
                    {request},
                    timeout=min(0.05, execution_context.remaining_seconds()),
                )
            execution_context.check_active()
            return await request
        except APIStatusError as exc:
            execution_context.check_active()
            if _context_overflow(exc):
                raise ProviderContextOverflow("provider context window exceeded") from exc
            raise RuntimeError(
                f"Provider HTTP {exc.status_code}: {exc.message}"
            ) from exc
        except (APIConnectionError, APITimeoutError) as exc:
            execution_context.check_active()
            raise RuntimeError(f"Provider transport failed: {exc}") from exc
        finally:
            if not request.done():
                request.cancel()
            # Await cancellation so the active response stream closes before return.
            await asyncio.gather(request, return_exceptions=True)

    def close(self):
        if self._closed:
            return
        try:
            if self._sdk_client is not None:
                self._runner.run(self._sdk_client.close())
        finally:
            self._sdk_client = None
            self._runner.close()
            self._closed = True

    def complete_turn(self, messages, max_output_tokens, *, system_prompt,
                      action_tools, execution_context: ExecutionContext):
        if self._pending_call_ids:
            raise RuntimeError("pending function calls have no recorded outputs")
        try:
            response = self._request(
                self._payload(
                    messages,
                    max_output_tokens,
                    system_prompt,
                    action_tools,
                ),
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
            self._pending_call_ids = turn.pending_call_ids
        return AssistantTurn(turn.action, turn.text, turn.usage)
