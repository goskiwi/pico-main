#!/usr/bin/env python3
import argparse
import html
import json
from pathlib import Path


def load_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_trace(path):
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def pill(value, class_name=""):
    class_attr = f" {class_name}" if class_name else ""
    return f'<span class="pill{class_attr}">{esc(value)}</span>'


def metric(label, value):
    return f"""
    <div class="metric">
      <div class="metric-label">{esc(label)}</div>
      <div class="metric-value">{esc(value)}</div>
    </div>
    """


def trace_duration(events):
    started = next((item for item in events if item.get("event") == "run_started"), {})
    finished = next((item for item in reversed(events) if item.get("event") == "run_finished"), {})
    duration = finished.get("run_duration_ms")
    if duration is not None:
        return f"{duration} ms"
    return f"{len(events)} events" if started or finished else "-"


def usage_metrics(prompt_metadata):
    fields = [
        ("Prompt chars", prompt_metadata.get("prompt_chars")),
        ("Estimated tokens", prompt_metadata.get("prompt_estimated_tokens")),
        ("Input tokens", prompt_metadata.get("input_tokens")),
        ("Output tokens", prompt_metadata.get("output_tokens")),
        ("Cached tokens", prompt_metadata.get("cached_tokens")),
        ("Cache hit", prompt_metadata.get("cache_hit")),
    ]
    return "".join(metric(label, "-" if value in (None, "") else value) for label, value in fields)


def render_tool_audit(tool_audit):
    if not tool_audit:
        return '<p class="empty">No tool calls recorded.</p>'
    rows = []
    for index, item in enumerate(tool_audit, start=1):
        status = str(item.get("status", ""))
        status_class = "ok" if status == "ok" else "warn" if status in {"dry_run", "partial_success"} else "bad" if status else ""
        command_or_path = item.get("command") or item.get("path") or ""
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{esc(item.get('name', ''))}</td>"
            f"<td>{pill(status or '-', status_class)}</td>"
            f"<td>{esc(item.get('capability', ''))}</td>"
            f"<td>{esc(item.get('duration_ms', 0))} ms</td>"
            f"<td>{esc(command_or_path)}</td>"
            f"<td>{esc(', '.join(item.get('affected_paths') or []))}</td>"
            f"<td>{esc(item.get('approval_decision', ''))}</td>"
            f"<td>{esc(item.get('shell_policy_reason', ''))}</td>"
            "</tr>"
        )
    return """
    <table>
      <thead>
        <tr>
          <th>#</th><th>Tool</th><th>Status</th><th>Capability</th><th>Duration</th>
          <th>Command / Path</th><th>Affected paths</th><th>Approval</th><th>Shell policy</th>
        </tr>
      </thead>
      <tbody>
    """ + "\n".join(rows) + """
      </tbody>
    </table>
    """


def render_security_events(summary, tool_audit):
    events = list((summary or {}).get("security_events") or [])
    for item in tool_audit:
        event_type = item.get("security_event_type")
        if event_type and not any(event.get("type") == event_type and event.get("name") == item.get("name") for event in events):
            events.append(
                {
                    "name": item.get("name", ""),
                    "type": event_type,
                    "error_code": item.get("error_code", ""),
                }
            )
    if not events:
        return '<p class="empty">No security events recorded.</p>'
    rows = [
        f"<tr><td>{esc(item.get('name', ''))}</td><td>{esc(item.get('type', ''))}</td><td>{esc(item.get('error_code', ''))}</td></tr>"
        for item in events
    ]
    return """
    <table>
      <thead><tr><th>Tool</th><th>Event type</th><th>Error code</th></tr></thead>
      <tbody>
    """ + "\n".join(rows) + """
      </tbody>
    </table>
    """


def render_trace(events):
    if not events:
        return '<p class="empty">No trace events recorded.</p>'
    items = []
    for item in events:
        event = item.get("event", "")
        details = json.dumps(item, indent=2, sort_keys=True, ensure_ascii=True)
        items.append(
            f"""
            <details class="trace-item">
              <summary><span>{esc(event)}</span><time>{esc(item.get('created_at', ''))}</time></summary>
              <pre>{esc(details)}</pre>
            </details>
            """
        )
    return "\n".join(items)


def load_task_graph(run_dir, report):
    path = report.get("task_graph_path") or str(Path(run_dir) / "task_graph.mmd")
    graph_path = Path(path)
    if not graph_path.is_absolute():
        graph_path = Path(run_dir) / graph_path
    if not graph_path.exists() or not graph_path.is_file():
        return "", str(graph_path)
    return graph_path.read_text(encoding="utf-8", errors="replace"), str(graph_path)


def render_task_graph(graph_text, graph_path):
    if not str(graph_text).strip():
        return '<p class="empty">No task graph recorded.</p>'
    return f"""
    <p class="subtle">{esc(graph_path)}</p>
    <pre>{esc(graph_text)}</pre>
    """


def render_html(run_dir):
    run_dir = Path(run_dir)
    report = load_json(run_dir / "report.json", default={}) or {}
    task_state = load_json(run_dir / "task_state.json", default={}) or {}
    trace = load_trace(run_dir / "trace.jsonl")
    summary = report.get("summary") or {}
    prompt_metadata = report.get("prompt_metadata") or {}
    tool_audit = report.get("tool_audit") or []
    status = report.get("status") or task_state.get("status") or "unknown"
    stop_reason = report.get("stop_reason") or task_state.get("stop_reason") or "-"
    final_answer = report.get("final_answer") or task_state.get("final_answer") or ""
    changed_files = summary.get("changed_files") or []
    failed_tools = summary.get("failed_tools") or []
    task_graph, task_graph_path = load_task_graph(run_dir, report)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>pico run report - {esc(run_dir.name)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --line: #d9dee8;
      --ok: #137a4b;
      --warn: #9a5b00;
      --bad: #b42318;
      --accent: #2557a7;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    header {{ margin-bottom: 22px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 18px; letter-spacing: 0; }}
    .subtle {{ color: var(--muted); font-size: 14px; }}
    .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 18px 0; }}
    .metric, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }}
    .metric {{ padding: 14px; min-height: 82px; }}
    .metric-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    .metric-value {{ margin-top: 8px; font-size: 20px; font-weight: 650; overflow-wrap: anywhere; }}
    section {{ padding: 18px; margin: 14px 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    pre {{
      margin: 10px 0 0;
      padding: 12px;
      overflow: auto;
      background: #111827;
      color: #f9fafb;
      border-radius: 6px;
      font-size: 12px;
    }}
    .pill {{
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      background: #f9fafb;
    }}
    .pill.ok {{ color: var(--ok); border-color: rgba(19, 122, 75, .35); background: rgba(19, 122, 75, .08); }}
    .pill.warn {{ color: var(--warn); border-color: rgba(154, 91, 0, .35); background: rgba(154, 91, 0, .08); }}
    .pill.bad {{ color: var(--bad); border-color: rgba(180, 35, 24, .35); background: rgba(180, 35, 24, .08); }}
    .list {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .empty {{ color: var(--muted); margin: 0; }}
    .trace-item {{ border-top: 1px solid var(--line); padding: 10px 0; }}
    .trace-item:first-child {{ border-top: 0; }}
    summary {{ cursor: pointer; display: flex; justify-content: space-between; gap: 12px; }}
    time {{ color: var(--muted); font-size: 12px; }}
    @media (max-width: 860px) {{
      main {{ padding: 18px; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      table {{ display: block; overflow-x: auto; }}
    }}
    @media (max-width: 520px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>pico Run Report</h1>
    <div class="subtle">{esc(run_dir.name)} · {esc(run_dir)}</div>
  </header>

  <div class="grid">
    {metric("Status", status)}
    {metric("Stop reason", stop_reason)}
    {metric("Attempts", report.get("attempts", task_state.get("attempts", "-")))}
    {metric("Tool steps", report.get("tool_steps", task_state.get("tool_steps", "-")))}
    {metric("Duration", trace_duration(trace))}
    {metric("Dry run", report.get("dry_run", False))}
    {metric("Prompt chars", prompt_metadata.get("prompt_chars", "-"))}
    {metric("Estimated tokens", prompt_metadata.get("prompt_estimated_tokens", "-"))}
  </div>

  <section>
    <h2>Summary</h2>
    <p><strong>Task:</strong> {esc(summary.get("task") or task_state.get("user_request") or "-")}</p>
    <p><strong>Final answer:</strong> {esc(final_answer or "-")}</p>
    <p><strong>Changed files:</strong></p>
    <div class="list">{''.join(pill(path) for path in changed_files) or '<span class="empty">none</span>'}</div>
    <p><strong>Failed tools:</strong></p>
    <div class="list">{''.join(pill(f"{item.get('name', '')}: {item.get('error_code', '')}", "bad") for item in failed_tools) or '<span class="empty">none</span>'}</div>
  </section>

  <section>
    <h2>Tool Timeline</h2>
    {render_tool_audit(tool_audit)}
  </section>

  <section>
    <h2>Task Graph</h2>
    {render_task_graph(task_graph, task_graph_path)}
  </section>

  <section>
    <h2>Safety</h2>
    {render_security_events(summary, tool_audit)}
  </section>

  <section>
    <h2>Context And Cost</h2>
    <div class="grid">{usage_metrics(prompt_metadata)}</div>
  </section>

  <section>
    <h2>Trace Events</h2>
    {render_trace(trace)}
  </section>
</main>
</body>
</html>
"""


def render_run(run_dir, output=None):
    run_dir = Path(run_dir)
    if not (run_dir / "report.json").exists() and not (run_dir / "trace.jsonl").exists():
        raise FileNotFoundError(f"not a pico run directory: {run_dir}")
    output = Path(output) if output else run_dir / "report.html"
    output.write_text(render_html(run_dir), encoding="utf-8")
    return output


def run_dirs(root):
    root = Path(root)
    return sorted(
        [path for path in root.iterdir() if path.is_dir() and ((path / "report.json").exists() or (path / "trace.jsonl").exists())],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def render_index(runs_root, rendered):
    rows = []
    for run_dir, html_path in rendered:
        report = load_json(run_dir / "report.json", default={}) or {}
        summary = report.get("summary") or {}
        rows.append(
            "<tr>"
            f"<td><a href=\"{esc(html_path.name)}\">{esc(run_dir.name)}</a></td>"
            f"<td>{esc(report.get('status', ''))}</td>"
            f"<td>{esc(report.get('stop_reason', ''))}</td>"
            f"<td>{esc(summary.get('task', ''))}</td>"
            "</tr>"
        )
    index = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>pico run reports</title>
  <style>
    body {{ margin: 0; padding: 28px; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fb; color: #1f2937; }}
    main {{ max-width: 1100px; margin: 0 auto; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d9dee8; border-radius: 8px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid #d9dee8; padding: 10px; text-align: left; vertical-align: top; }}
    th {{ color: #6b7280; font-size: 12px; text-transform: uppercase; }}
    a {{ color: #2557a7; text-decoration: none; }}
  </style>
</head>
<body>
<main>
  <h1>pico Run Reports</h1>
  <table>
    <thead><tr><th>Run</th><th>Status</th><th>Stop reason</th><th>Task</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</main>
</body>
</html>
"""
    output = Path(runs_root) / "index.html"
    output.write_text(index, encoding="utf-8")
    return output


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Render pico run artifacts as static HTML.")
    parser.add_argument("path", help="A .pico/runs/<run_id> directory, or a .pico/runs root with --all/--latest.")
    parser.add_argument("--output", default=None, help="Output HTML path for a single run. Defaults to report.html in the run directory.")
    parser.add_argument("--all", action="store_true", help="Render every run directory under the given runs root and write index.html.")
    parser.add_argument("--latest", action="store_true", help="Render only the newest run under the given runs root.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    path = Path(args.path)
    if args.all:
        rendered = [(run_dir, render_run(run_dir)) for run_dir in run_dirs(path)]
        index_path = render_index(path, rendered)
        print(index_path)
        return 0
    if args.latest:
        candidates = run_dirs(path)
        if not candidates:
            raise SystemExit(f"no run directories found under {path}")
        print(render_run(candidates[0], args.output))
        return 0
    print(render_run(path, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
