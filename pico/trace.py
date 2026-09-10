"""Small content-free live trace for one Pico run."""

import json
from datetime import datetime, timezone
from pathlib import Path


class TracePrinter:
    def __init__(self, stream):
        self.stream = stream
        self.path = None
        self.redactor = lambda value: value

    def bind(self, path, redactor):
        self.path = Path(path)
        self.redactor = redactor
        return self

    def event(self, kind, payload=None):
        safe = self.redactor(payload or {})
        data = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "time": datetime.now(timezone.utc).isoformat(),
                "event": kind,
                "data": safe,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        if self.stream is None:
            return
        try:
            print(f"[pico:{kind}] {data}", file=self.stream, flush=True)
        except (OSError, ValueError):
            self.stream = None

    def __call__(self, kind, payload=None):
        self.event(kind, payload)
