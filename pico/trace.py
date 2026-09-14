"""Live, content-free terminal view of accepted Run events."""

import json
import time


class TracePrinter:
    def __init__(self, stream):
        self.stream = stream
        self.run_id = None
        self.turn = 0
        self.request_started = None
        self.calls = {}

    def write(self, message):
        if self.stream is None:
            return
        try:
            print(message, file=self.stream, flush=True)
        except (OSError, ValueError):
            # A closed trace destination must not interrupt a committed Run.
            self.stream = None

    @staticmethod
    def _label(call):
        name = call["name"] if isinstance(call, dict) else call.name
        args = call["args"] if isinstance(call, dict) else call.args
        path = args.get("path")
        suffix = " " + json.dumps(path, ensure_ascii=True) if isinstance(path, str) else ""
        return name + suffix

    def __call__(self, event):
        if self.stream is None:
            return
        if self.run_id != event.run_id:
            self.run_id = event.run_id
            self.turn = 0
            self.request_started = None
            self.calls = {}
            self.write(f"[Trace Run] {event.run_id}")
        message = self._message(event)
        if message:
            self.write(message)

    def _message(self, event):
        payload = event.payload
        kind = event.kind
        if kind == "model_requested":
            self.turn += 1
            self.request_started = time.monotonic()
            return f"[Model {self.turn}] requesting…"
        if kind in {"assistant_turn", "model_failure"}:
            elapsed = time.monotonic() - self.request_started if self.request_started else 0
            if kind == "assistant_turn":
                turn = payload["turn"]
                usage = turn.get("usage", {})
                action = turn.get("action", {})
                self.calls = {
                    call["call_id"]: call
                    for call in action.get("tool_calls", [])
                }
            else:
                usage = payload.get("usage", {})
                action = {"kind": payload.get("kind")}
            cached = usage.get("cached_tokens")
            cached_text = "unknown" if cached is None else str(cached)
            status = action.get("kind", "unknown")
            return (f"[Model {self.turn}] {status} · input={usage.get('input_tokens')} "
                    f"cached={cached_text} "
                    f"output={usage.get('output_tokens')} · {elapsed:.2f}s")
        if kind == "tool_started":
            call = self.calls.get(event.call_id)
            label = self._label(call) if call else event.call_id
            return f"[Tool {event.call_id}] {label} · started"
        if kind == "tool_result":
            outcome = payload['outcome']
            call = self.calls.pop(event.call_id, None)
            label = (
                self._label(call)
                if call
                else outcome['tool_name']
            )
            return (f"[Tool {event.call_id}] {label} · {outcome['status']} "
                    f"· effect={outcome['side_effect_state']}")
        if kind == "compaction":
            return (
                "[Compaction] committed · through="
                f"{event.covered_through_sequence}"
            )
        if kind == "provider_session_reset":
            return f"[Provider] session reset · {payload.get('reason')}"
        if kind in {"run_started", "run_resumed"}:
            return f"[Run] {kind.removeprefix('run_')}"
        if kind == "run_stopped":
            status = payload.get('stop_reason')
            return (
                f"[Run] {status} · "
                f"{payload.get('attempt_duration_ms', 0) / 1000:.2f}s"
            )
        return None
