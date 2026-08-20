"""Stable prompt prefix construction."""

import hashlib
import textwrap
from dataclasses import dataclass


@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str
    hash: str


def build_prompt_prefix(workspace, tools):
    tool_lines = []
    for name, tool in tools.items():
        risk = "approval required" if tool["risky"] else "safe"
        tool_lines.append(f"- {name} [{risk}] {tool['description']}")
    tool_text = "\n".join(tool_lines)
    # prefix 可以理解成 agent 的“工作手册”：
    # 它是谁、工具怎么调用、当前仓库是什么状态，都写在这里。
    text = textwrap.dedent(
        f"""\
        You are pico, a small local coding agent working inside a local repository.

        Rules:
        - Use tools instead of guessing about the workspace.
        - Use exactly one provided function per turn.
        - Call submit_final only after the task is complete.
        - Never invent tool results.
        - Keep answers concise and concrete.
        - If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.
        - Before writing tests for existing code, read the implementation first.
        - Use git commands only when the workspace is a Git repository.
        - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
        - New files should be complete and runnable, including obvious imports.
        - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
        - Required tool arguments must not be empty.

        Tools:
        {tool_text}

        {workspace.text()}
        """
    ).strip()
    return PromptPrefix(
        text=text,
        hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
