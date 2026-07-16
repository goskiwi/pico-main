"""Transcript rendering and history-compaction policy."""

import json
import re

from .config import (
    HISTORY_RECENT_WINDOW,
    LLM_COMPACT_MAX_INPUT_CHARS,
    LLM_COMPACT_MAX_OUTPUT_TOKENS,
    MAX_HISTORY,
)
from .context_types import SectionRender, _estimate_tokens, _indent_block, _tail_clip, _token_clip
from .workspace import clip


SHELL_IMPORTANT_LINE_PATTERN = re.compile(
    r"(?i)(\b(error|failed|failure|traceback|exception|assert|assertion|timeout)\b|assertionerror|exit_code:\s*[1-9])"
)


class HistoryRenderer:
    def __init__(self, agent):
        self.agent = agent

    def history_text(self):
        history = self.agent.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - HISTORY_RECENT_WINDOW)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(str(item.get("summary") or item.get("content", "")), limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def _render_history_section(self, budget, llm_history_compaction_enabled=False, dedup_file_paths=None):
        history = list(self.agent.session.get("history", []))
        raw = self._raw_history_text(history)
        if not history:
            rendered = "Transcript:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                    "dedup_skipped": 0,
                },
            )

        # history 超预算时，用模型把较旧的 transcript 改写成结构化接续摘要；
        # 最近窗口仍保留原始视图，给下一步决策留下刚发生的工具证据。
        recent_window = HISTORY_RECENT_WINDOW
        recent_start = max(0, len(history) - recent_window)
        older_history = history[:recent_start]
        recent_history = history[recent_start:]
        compact_summary = ""
        compact_error = ""
        compact_used = False
        fallback_details = {
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        if llm_history_compaction_enabled and older_history and _estimate_tokens(raw) > budget:
            try:
                compact_summary = self._llm_compact_history(older_history)
                compact_used = bool(compact_summary)
            except Exception as exc:
                compact_error = str(exc)

        if not compact_summary and older_history:
            compact_summary, fallback_details = self._fallback_compact_summary(older_history)

        # 近期历史使用智能工具摘要，而非原样输出
        dedup = dedup_file_paths or set()
        dedup_skipped = 0
        recent_lines = []
        for item in recent_history:
            if item.get("role") == "tool":
                summary = self._summarize_tool_item(item)
                if dedup and item.get("name") == "read_file":
                    path = str(item.get("args", {}).get("path", "")).strip()
                    if path in dedup:
                        summary = f"[tool:read_file] {path} -> (already in memory summary)"
                        dedup_skipped += 1
                recent_lines.append(summary)
            else:
                recent_lines.extend(self._render_history_item(item, 200))

        rendered = self._render_compacted_transcript(
            compact_summary=compact_summary,
            recent_lines=recent_lines,
            budget=budget,
        )

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered.splitlines()[1:],
                "older_entries_count": len(older_history),
                **fallback_details,
                "llm_compact_used": compact_used,
                "llm_compact_error": compact_error,
                "llm_compact_summary_chars": len(compact_summary),
                "dedup_skipped": dedup_skipped,
            },
        )

    def _render_compacted_transcript(self, compact_summary, recent_lines, budget):
        if budget <= 0:
            budget = 1_000_000

        selected_recent = []
        for line in reversed(recent_lines):
            candidate = [line] + selected_recent
            reserved_recent = "\n".join(["Recent transcript:", *candidate])
            fixed = "Transcript:\n"
            if compact_summary:
                fixed += "Task graph:\n"
            if _estimate_tokens(fixed + reserved_recent) <= budget:
                selected_recent = candidate
                continue
            if not selected_recent:
                available = max(1, budget - _estimate_tokens(fixed + "Recent transcript:\n"))
                selected_recent = [_token_clip(line, available)]
            break

        recent_block = "\n".join(["Recent transcript:", *selected_recent]) if selected_recent else ""
        available_summary = budget - _estimate_tokens("Transcript:\n") - _estimate_tokens(recent_block)
        if compact_summary:
            available_summary -= _estimate_tokens("Task graph:\n\n")
        compact_summary = _token_clip(compact_summary, max(1, available_summary)) if compact_summary else ""

        lines = ["Transcript:"]
        if compact_summary:
            lines.extend(["Task graph:", *_indent_block(compact_summary)])
        if recent_block:
            lines.append(recent_block)
        rendered = "\n".join(lines)
        if _estimate_tokens(rendered) > budget:
            rendered = _token_clip(rendered, budget)
        return rendered

    def _llm_compact_history(self, older_history):
        prompt = self._compact_prompt(older_history)
        output = self.agent.model_client.complete(
            prompt,
            max_new_tokens=LLM_COMPACT_MAX_OUTPUT_TOKENS,
            purpose="history_compact",
        )
        return self._sanitize_compact_output(output)

    def _compact_prompt(self, older_history):
        memory_text = str(self.agent.memory_text())
        transcript = _tail_clip(self._raw_history_text(older_history), LLM_COMPACT_MAX_INPUT_CHARS)
        return "\n".join(
            [
                "You are compacting a coding agent transcript.",
                "Respond with TEXT ONLY. Do not call tools. Do not invent facts.",
                "Preserve concrete state needed to continue the task.",
                "",
                "Write a Mermaid flowchart TD task graph only.",
                "Rules:",
                "- The first non-empty line must be: flowchart TD",
                "- Use node labels shaped as: type | status | summary | ref: optional_path",
                "- Prefer node types: goal, task, tool, finding, decision, error, next",
                "- Prefer statuses: open, done, blocked",
                "- Preserve content_ref paths from tool history when present.",
                "- Keep the graph small: at most 12 nodes and 16 edges.",
                "",
                "Example:",
                "flowchart TD",
                "  G[\"goal | open | Fix failing tests\"]",
                "  T001[\"tool | done | read_file tests/test_pico.py | ref: .pico/runs/run_x/tool_outputs/0001_read_file.txt\"]",
                "  N1[\"next | open | Run targeted pytest\"]",
                "  G --> T001 --> N1",
                "",
                "Current memory:",
                memory_text or "- none",
                "",
                "Older transcript to compact:",
                transcript,
            ]
        )

    def _sanitize_compact_output(self, output):
        text = str(output or "").strip()
        text = re.sub(r"</?final>", "", text).strip()
        text = re.sub(r"</?tool[^>]*>", "", text).strip()
        text = re.sub(r"<[^>\n]+>", "", text)
        text = text.replace("`", "")
        if not text:
            return ""
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        start = next((index for index, line in enumerate(lines) if line.strip().startswith("flowchart TD")), -1)
        if start < 0:
            lines = ["flowchart TD", '  N1["next | open | Continue from recent transcript"]']
        else:
            lines = lines[start:]
        sanitized = []
        node_count = 0
        edge_count = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if not sanitized:
                sanitized.append("flowchart TD")
                continue
            if "-->" in stripped:
                if edge_count >= 16:
                    continue
                edge_count += 1
                sanitized.append("  " + _tail_clip(stripped, 160))
                continue
            if "[" in stripped and "]" in stripped:
                if node_count >= 12:
                    continue
                node_count += 1
                sanitized.append("  " + _tail_clip(stripped, 220))
                continue
        if len(sanitized) == 1:
            sanitized.append('  N1["next | open | Continue from recent transcript"]')
        return _tail_clip("\n".join(sanitized), 2400)

    def _fallback_compact_summary(self, older_history):
        lines = []
        seen_reads = set()
        details = {
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }
        for item in older_history:
            if item.get("role") == "tool":
                if item.get("name") == "read_file":
                    path = str(item.get("args", {}).get("path", "")).strip()
                    if path in seen_reads:
                        details["collapsed_duplicate_reads"] += 1
                        continue
                    seen_reads.add(path)
                    summary = self._reusable_file_summary(path)
                    if summary:
                        lines.append(f"- {path} -> {summary}")
                        details["reused_file_summary_count"] += 1
                        continue
                lines.append(f"- {self._summarize_old_tool_item(item)}")
                details["summarized_tool_count"] += 1
            else:
                rendered = self._render_history_item(item, 30)
                lines.extend(f"- {line}" for line in rendered)
        return ("\n".join(lines) if lines else "- none"), details

    def _reusable_file_summary(self, path):
        snapshot = self.agent.memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), {})
        if not summary:
            return ""
        return str(summary.get("summary", "")).strip()

    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            raw = str(item.get("summary") or item.get("content", ""))
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            important = [line for line in lines if SHELL_IMPORTANT_LINE_PATTERN.search(line)]
            selected = important[:3] if important else lines[:3]
            summary = " | ".join(selected) if selected else "(empty)"
            return f"{command} -> {summary}"
        return self._render_history_item(item, 60)[0]

    def _summarize_tool_item(self, item):
        """为近期历史中的工具调用生成智能摘要。

        如果 history item 已经带了预计算的 summary（磁盘存储模式），
        直接使用；否则从 content 字段生成摘要。
        """
        name = item.get("name", "")
        args = item.get("args", {})
        # 优先使用预计算的 summary（磁盘存储模式）
        precomputed = item.get("summary")
        if precomputed:
            return f"[tool:{name}] {precomputed}"
        content = str(item.get("content", ""))

        if name == "read_file":
            path = str(args.get("path", ""))
            start = args.get("start", 1)
            end = args.get("end", 0)
            lines = content.splitlines() if content else []
            header = f"[tool:read_file] {path} (lines {start}-{end}, {len(lines)} lines)"
            preview = self._first_lines(content, 8)
            if preview:
                extra = len(lines) - preview.count("\n") - 1
                suffix = f"\n  ... ({extra} more lines)" if extra > 0 else ""
                return f"{header}\n  {preview}{suffix}"
            return header

        if name == "run_shell":
            command = str(args.get("command", "")).strip() or "shell"
            lines = [line for line in content.splitlines() if line.strip()]
            important = [line for line in lines if SHELL_IMPORTANT_LINE_PATTERN.search(line)]
            selected = important[:5] if important else lines[:3]
            summary = " | ".join(selected[:3]) if selected else "(empty)"
            return f"[tool:run_shell] {command} -> {summary}"

        if name in ("write_file", "patch_file"):
            path = str(args.get("path", ""))
            status = "(ok)" if content and "error" not in content.lower() else "(error)"
            return f"[tool:{name}] {path} {status}"

        if name in ("list_files", "search"):
            lines = [line for line in content.splitlines() if line.strip()][:5]
            summary = " | ".join(lines) if lines else "(empty)"
            return f"[tool:{name}] {summary}"

        return f"[tool:{name}] {_token_clip(content, 80)}"

    def _first_lines(self, content, n):
        if not content:
            return ""
        return "\n  ".join(content.splitlines()[:n])

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item.get("summary") or item.get("content", "")))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = str(item.get("summary") or item.get("content", ""))
            content = _token_clip(content, max(20, line_limit))
            return [prefix, content]
        return [f"[{item['role']}] {_token_clip(item['content'], line_limit)}"]
