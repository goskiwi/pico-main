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
        path = call.args.get("path")
        suffix = " " + json.dumps(path, ensure_ascii=True) if isinstance(path, str) else ""
        return call.name + suffix

    def __call__(self, event):
        if self.stream is None:
            return
        if self.run_id != event.run_id:
            self.run_id = event.run_id
            self.turn = 0
            self.request_started = None
            self.calls.clear()
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
        if kind == "turn_metrics":
            elapsed = time.monotonic() - self.request_started if self.request_started else 0
            return (f"[Model {self.turn}] returned · input={payload.get('input_tokens')} "
                    f"output={payload.get('output_tokens')} · {elapsed:.2f}s")
        if kind == "assistant_tool_calls":
            self.calls = {call.call_id: call for call in event.tool_calls}
            return f"[Tools] accepted {len(self.calls)} call(s)"
        if kind == "tool_started":
            call = self.calls.get(event.call_id)
            label = self._label(call) if call else payload['tool_name']
            return f"[Tool {event.call_id}] {label} · started"
        if kind == "tool_result":
            outcome = payload['outcome']
            call = self.calls.get(event.call_id)
            label = self._label(call) if call else outcome['tool_name']
            return (f"[Tool {event.call_id}] {label} · {outcome['status']} "
                    f"· effect={outcome['side_effect_state']}")
        if kind == "compaction":
            return f"[Compaction] committed · covered={len(event.covered_event_ids)}"
        if kind == "provider_session_reset":
            return f"[Provider] session reset · {payload.get('reason')}"
        if kind == "verification_result":
            return f"[Verification] {payload['status']} · exit={payload.get('exit_code')}"
        if kind == "completion_blocked":
            return f"[Completion] blocked · {payload.get('status')}"
        if kind in {"run_started", "run_resumed"}:
            return f"[Run] {kind.removeprefix('run_')}"
        if kind in {"assistant_final", "run_stopped"}:
            status = "completed" if kind == "assistant_final" else payload.get('stop_reason')
            return f"[Run] {status} · {payload.get('turn_duration_ms', 0) / 1000:.2f}s"
        return None
