"""模型输出解析器。

把模型返回的原始文本解析成 runtime 可执行的 (kind, payload) 元组。
kind 可能是 "tool"、"final" 或 "retry"。

这个模块从 runtime.py 拆出来，让解析逻辑和 agent 循环解耦。
"""

import json
import re


def retry_notice(problem=None):
    """构造一条重试提示，告诉模型输出格式不对。"""
    prefix = "Runtime notice"
    if problem:
        prefix += f": {problem}"
    else:
        prefix += ": model returned malformed tool output"
    return (
        f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
        'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
    )


def extract_tag(text, tag):
    """提取闭合的 <tag>...</tag> 内容，前后去除空白。"""
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = text.find(start_tag)
    if start == -1:
        return None
    start += len(start_tag)
    end = text.find(end_tag, start)
    if end == -1:
        return None
    return text[start:end].strip()


def extract_raw(text, tag):
    """和 extract_tag 一样，但不去除前后空白。"""
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = text.find(start_tag)
    if start == -1:
        return None
    start += len(start_tag)
    end = text.find(end_tag, start)
    if end == -1:
        return None
    return text[start:end]


def parse_attrs(text):
    """从 XML 风格属性字符串中解析出 key=value 对。"""
    attrs = {}
    for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
        attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return attrs


def parse_xml_tool(raw):
    """从文本中解析 XML 风格的工具调用。

    格式：<tool name="tool_name" path="file.py"><content>...</content></tool>
    返回 {"name": ..., "args": ...} 或 None。
    """
    match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
    if not match:
        return None
    attrs = parse_attrs(match.group("attrs"))
    name = str(attrs.pop("name", "")).strip()
    if not name:
        return None

    body = match.group("body")
    args = dict(attrs)
    for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
        if f"<{key}>" in body:
            value = extract_raw(body, key)
            if value is None:
                return None
            args[key] = value

    body_text = body.strip("\n")
    if name == "write_file" and "content" not in args and body_text:
        args["content"] = body_text
    if name == "delegate" and "task" not in args and body_text:
        args["task"] = body_text.strip()
    return {"name": name, "args": args}


def parse_model_output(raw, *, require_explicit_final=False):
    """把模型原始输出解析成 runtime 可执行的动作或最终答案。

    为什么存在：
    模型输出首先是自然语言文本，而 runtime 需要的是结构化决策：
    "这是工具调用"还是"这是最终答案"。如果没有这层解析，后面的工具校验、
    审批和执行链路就没法可靠工作。

    输入 / 输出：
    - 输入：模型返回的原始文本 `raw`
    - 输出：`(kind, payload)`，其中 `kind` 可能是 `tool`、`final`、`retry`

    在 agent 链路里的位置：
    它位于 `model_client.complete()` 之后、`run_tool()` 之前，是模型输出
    进入平台控制流的第一道结构化关口。
    """
    raw = str(raw)
    # 这里支持两种工具格式：
    # 1. <tool>...</tool> 里包 JSON，适合简短调用
    # 2. XML 风格属性/子标签，适合写文件这类多行内容
    if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
        body = extract_tag(raw, "tool")
        if body is None:
            return "retry", retry_notice("model returned an unclosed <tool> call")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return "retry", retry_notice("model returned malformed tool JSON")
        if not isinstance(payload, dict):
            return "retry", retry_notice("tool payload must be a JSON object")
        if not str(payload.get("name", "")).strip():
            return "retry", retry_notice("tool payload is missing a tool name")
        args = payload.get("args", {})
        if args is None:
            payload["args"] = {}
        elif not isinstance(args, dict):
            return "retry", retry_notice()
        return "tool", payload
    if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
        payload = parse_xml_tool(raw)
        if payload is not None:
            return "tool", payload
        return "retry", retry_notice()
    if "<final>" in raw:
        final = extract_tag(raw, "final")
        if final is None:
            return "retry", retry_notice("model returned an unclosed <final> answer")
        final = final.strip()
        if final:
            return "final", final
        return "retry", retry_notice("model returned an empty <final> answer")
    raw = raw.strip()
    if raw:
        if require_explicit_final:
            return "retry", retry_notice(
                "bare text is not a final answer; wrap completion in <final>...</final>"
            )
        return "final", raw
    return "retry", retry_notice("model returned an empty response")
