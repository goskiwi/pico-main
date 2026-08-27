"""Day 3: inspect prompt assembly and native Responses continuation."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from pico import (
    FakeModelClient,
    ModelAction,
    OpenAICompatibleModelClient,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload)
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload.encode("utf-8")


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day3-") as directory:
        root = Path(directory)
        (root / "README.md").write_text(
            "# Provider demo\n\nRead this file through a native function call.\n",
            encoding="utf-8",
        )
        bootstrap = Pico(
            model_client=FakeModelClient([]),
            workspace=WorkspaceContext.build(root),
            session_store=SessionStore(root / ".pico" / "sessions"),
            config=PicoConfig(
                approval_policy="auto",
                verification_command="",
                max_new_tokens=96,
            ),
        )
        prompt, metadata = bootstrap.prompt.build("Read README.md")
        action_tools = bootstrap.tools.action_schemas
        read_schema = next(
            tool for tool in action_tools if tool["name"] == "read_file"
        )

        requests = []

        def urlopen(request, timeout):
            requests.append(
                {
                    "timeout": timeout,
                    "payload": json.loads(request.data.decode("utf-8")),
                }
            )
            if len(requests) == 1:
                return Response(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "read_file",
                                "call_id": "call_readme",
                                "arguments": json.dumps(
                                    {
                                        "path": "README.md",
                                        "start_line": 1,
                                        "end_line": 20,
                                    }
                                ),
                            }
                        ],
                        "usage": {
                            "input_tokens": 240,
                            "output_tokens": 18,
                            "total_tokens": 258,
                        },
                    }
                )
            return Response(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "submit_final",
                            "call_id": "call_final",
                            "arguments": json.dumps(
                                {"answer": "README was read through Responses."}
                            ),
                        }
                    ]
                }
            )

        client = OpenAICompatibleModelClient(
            model="gpt-demo",
            base_url="https://api.openai.com/v1",
            api_key="not-sent-to-a-network",
            temperature=None,
            timeout=5,
        )
        with patch("urllib.request.urlopen", urlopen):
            action = client.complete_action(
                prompt,
                96,
                action_tools=action_tools,
                prompt_cache_key=metadata["prompt_cache_key"],
            )
            client.record_action_result(
                action,
                '{"status":"success","content":"# Provider demo"}',
            )
            final = client.complete_action(
                "this replacement prompt must not enter an active continuation",
                96,
                action_tools=action_tools,
                prompt_cache_key=metadata["prompt_cache_key"],
            )

        first_input = requests[0]["payload"]["input"]
        continued_input = requests[1]["payload"]["input"]
        continued_types = [item.get("type", item.get("role")) for item in continued_input]

        assert action.kind == "tool"
        assert action.tool_call.name == "read_file"
        assert final == ModelAction.final("README was read through Responses.")
        assert read_schema["strict"] is True
        assert read_schema["parameters"]["additionalProperties"] is False
        assert set(read_schema["parameters"]["required"]) == {
            "path",
            "start_line",
            "end_line",
        }
        assert first_input[0]["content"][0]["text"] == prompt
        assert continued_types == [
            "user",
            "function_call",
            "function_call_output",
        ]
        assert "replacement prompt" not in json.dumps(continued_input)

        print_section(
            "Prompt 预算",
            {
                "section_order": metadata["section_order"],
                "sections": metadata["sections"],
                "prompt_tokens": metadata["prompt_tokens"],
                "reserved_output_tokens": metadata["reserved_output_tokens"],
                "prompt_tail": prompt[-240:],
            },
        )
        print_section(
            "Strict read_file schema",
            {
                "required": read_schema["parameters"]["required"],
                "additionalProperties": read_schema["parameters"][
                    "additionalProperties"
                ],
            },
        )
        print_section(
            "Responses 两轮输入",
            {
                "first_input_types": [
                    item.get("type", item.get("role")) for item in first_input
                ],
                "continued_input_types": continued_types,
                "tool_choice": requests[0]["payload"]["tool_choice"],
                "parallel_tool_calls": requests[0]["payload"][
                    "parallel_tool_calls"
                ],
                "prompt_cache_key_present": "prompt_cache_key"
                in requests[0]["payload"],
                "parsed_actions": [action.kind, final.kind],
            },
        )


if __name__ == "__main__":
    main()
