"""Session persistence for pico runtime state."""

import json
import tempfile
from pathlib import Path


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        path = self.path(session["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(session, handle, indent=2)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(path)
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        """Return the newest resumable session.

        Delegate sessions are audit artifacts, not interactive conversations,
        so ``--resume latest`` must not select them.
        """
        files = sorted(
            self.root.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files:
            try:
                session = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(session, dict):
                continue
            if session.get("session_kind") == "main":
                return path.stem
        return None
