"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from .actions import ModelAction, action_from_text
from .config import DEFAULT_MODEL_MAX_RETRIES, DEFAULT_MODEL_RETRY_BACKOFF


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.supports_prompt_cache = False
        self.supports_native_actions = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)

    def complete_action(self, prompt, max_new_tokens, *, require_explicit_final=False, **kwargs):
        kwargs.pop("action_tools", None)
        return action_from_text(
            self.complete(prompt, max_new_tokens, **kwargs),
            require_explicit_final=require_explicit_final,
            protocol="scripted_text",
        )


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.supports_native_actions = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        # Ollama 当前不支持我们这里接入的 prompt cache 语义，
        # 所以 runtime 传下来的缓存参数会被忽略。
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        return data.get("response", "")

    def complete_action(self, prompt, max_new_tokens, *, require_explicit_final=False, **kwargs):
        kwargs.pop("action_tools", None)
        return action_from_text(
            self.complete(prompt, max_new_tokens, **kwargs),
            require_explicit_final=require_explicit_final,
            protocol="ollama_text",
        )


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    return text

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            if event.get("type") == "response.completed":
                text = _extract_openai_text(response)
                if text:
                    return text, response
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text, last_response or {}
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event
    if deltas:
        return "".join(deltas), last_response or {}
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response), last_response
    return "", {}


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


def _read_response_body_with_retries(
    request,
    timeout,
    *,
    backend_name,
    reachability_message,
    attempts=DEFAULT_MODEL_MAX_RETRIES + 1,
):
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body_text = response.read().decode("utf-8")
                headers = getattr(response, "headers", {}) or {}
                content_type = headers.get("Content-Type", "")
            return body_text, content_type
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code >= 500 and attempt < attempts - 1:
                time.sleep(DEFAULT_MODEL_RETRY_BACKOFF * (attempt + 1))
                continue
            raise RuntimeError(f"{backend_name} request failed with HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, RemoteDisconnected) as exc:
            if attempt < attempts - 1:
                time.sleep(DEFAULT_MODEL_RETRY_BACKOFF * (attempt + 1))
                continue
            raise RuntimeError(reachability_message) from exc


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        # 当前只在明确支持 prompt cache 语义的后端上启用这条链路，
        # 避免对不支持的后端传一个“看起来统一、其实没意义”的伪参数。
        self.supports_prompt_cache = any(host in self.base_url for host in ("openai.com", "right.codes"))
        self.supports_native_actions = True
        self.last_completion_metadata = {}
        self.reset_action_session()

    def reset_action_session(self):
        self._action_input_items = []
        self._action_pending_call_id = ""
        self._action_pending_output = None

    def fork_for_delegate(self):
        """Create an independent Responses conversation for a child agent."""
        return OpenAICompatibleModelClient(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout=self.timeout,
        )

    def record_action_result(self, action, result):
        """Queue a tool or guard result for the pending Responses function call."""
        if not self._action_pending_call_id:
            return
        if action.call_id and action.call_id != self._action_pending_call_id:
            raise RuntimeError("action call_id does not match the pending Responses call")
        self._action_pending_output = str(result)

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        """向 OpenAI-compatible `/responses` 接口发起一次模型调用。

        为什么存在：
        runtime 不应该知道 HTTP 细节、SSE 细节、usage 字段长什么样，
        更不应该自己去判断 prompt cache 参数要不要带。这个函数把这些后端
        细节都包起来，对上层暴露统一的 `complete()` 行为。

        输入 / 输出：
        - 输入：完整 prompt、最大输出 token，以及可选的 prompt cache 参数
        - 输出：模型最终文本；同时把 usage / cached_tokens 等元数据写进
          `self.last_completion_metadata`

        在 agent 链路里的位置：
        它位于 `Pico.ask()` 的模型调用阶段，是稳定前缀缓存复用链路真正
        落到 provider API 的地方。
        """
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_output_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        _, text = self._responses_request(
            payload,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
        )
        if text:
            return text
        raise RuntimeError("OpenAI-compatible error: could not extract text from response")

    def complete_action(
        self,
        prompt,
        max_new_tokens,
        *,
        action_tools,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        require_explicit_final=False,
    ):
        """Request exactly one strict function call and normalize it to ``ModelAction``."""
        del require_explicit_final
        if not self._action_input_items:
            self._action_input_items = [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ]
        if self._action_pending_call_id:
            if self._action_pending_output is None:
                raise RuntimeError("pending Responses function call has no recorded output")
            self._action_input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": self._action_pending_call_id,
                    "output": self._action_pending_output,
                }
            )
        payload = {
            "model": self.model,
            "input": list(self._action_input_items),
            "max_output_tokens": max_new_tokens,
            "stream": False,
            "tools": list(action_tools),
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "store": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        self._action_pending_output = None
        data, text = self._responses_request(
            payload,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
        )
        action = self._action_from_response(data, text, action_tools)
        self._action_input_items.extend(
            item for item in data.get("output", []) if isinstance(item, dict)
        )
        self._action_pending_call_id = action.call_id
        self.last_completion_metadata.update(
            {
                "action_protocol": action.protocol,
                "structured_action": True,
                "action_kind": action.kind,
            }
        )
        return action

    def _responses_request(self, payload, *, prompt_cache_key=None, prompt_cache_retention=None):
        self.last_completion_metadata = {}
        payload = dict(payload)
        # runtime 传入的是“稳定前缀”的签名，而不是整段 prompt 的签名。
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.supports_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        body_text, content_type = _read_response_body_with_retries(
            request,
            self.timeout,
            backend_name="OpenAI-compatible",
            reachability_message=(
                "Could not reach the OpenAI-compatible backend.\n"
                f"Base URL: {self.base_url}\n"
                f"Model: {self.model}"
            ),
        )

        # 有些兼容后端返回普通 JSON，有些返回 SSE。
        # 这里两种都接住，并尽量统一抽取文本和 usage/cache 元数据。
        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            text, response_data = _extract_openai_response_from_sse(body_text)
            if isinstance(response_data, dict) and response_data:
                # 这些元数据会一路传回 runtime，进入 trace 和 report，
                # 用来观察 prompt cache 是否真的命中。
                self.last_completion_metadata = {
                    "prompt_cache_supported": self.supports_prompt_cache,
                    "prompt_cache_key": prompt_cache_key,
                    "prompt_cache_retention": prompt_cache_retention,
                    **_extract_usage_cache_details(response_data),
                }
            if response_data or text:
                return response_data, text
            raise RuntimeError("OpenAI-compatible error: could not extract response from event stream")

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "OpenAI-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {data['error']}")
        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_usage_cache_details(data),
        }
        return data, _extract_openai_text(data)

    @staticmethod
    def _action_from_response(data, text, action_tools):
        protocol = "responses_function"
        calls = [item for item in data.get("output", []) if item.get("type") == "function_call"]
        raw_preview = text or json.dumps(data.get("output", []), ensure_ascii=False)
        if len(calls) != 1:
            return ModelAction.retry(
                f"expected exactly one function call, received {len(calls)}",
                protocol=protocol,
                raw_preview=raw_preview,
            )
        call = calls[0]
        name = str(call.get("name", "")).strip()
        call_id = str(call.get("call_id", "")).strip()
        try:
            args = json.loads(call.get("arguments", ""))
        except (TypeError, json.JSONDecodeError):
            return ModelAction.retry(
                f"function {name or '<missing>'} returned malformed JSON arguments",
                protocol=protocol,
                raw_preview=raw_preview,
                call_id=call_id,
            )
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
        allowed_names = {str(item.get("name", "")) for item in action_tools}
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


def _extract_anthropic_text(data):
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.supports_native_actions = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        # 为了保持统一接口，runtime 仍然会传缓存参数进来；
        # 这里只是显式丢弃，因为当前 Anthropic-compatible 路径没有接缓存复用。
        del prompt_cache_key, prompt_cache_retention
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        body_text, _ = _read_response_body_with_retries(
            request,
            self.timeout,
            backend_name="Anthropic-compatible",
            reachability_message=(
                "Could not reach the Anthropic-compatible backend.\n"
                f"Base URL: {self.base_url}\n"
                f"Model: {self.model}"
            ),
        )

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        text = _extract_anthropic_text(data)
        if text:
            return text
        raise RuntimeError("Anthropic-compatible error: could not extract text from response")

    def complete_action(self, prompt, max_new_tokens, *, require_explicit_final=False, **kwargs):
        kwargs.pop("action_tools", None)
        return action_from_text(
            self.complete(prompt, max_new_tokens, **kwargs),
            require_explicit_final=require_explicit_final,
            protocol="anthropic_text",
        )
