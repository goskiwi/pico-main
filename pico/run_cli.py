"""Show a Run summary or its persisted events."""

import argparse
import json

from .session_store import SessionStore
from .workspace import Workspace


def build_run_parser():
    parser = argparse.ArgumentParser(prog="pico run")
    parser.add_argument("command", choices=("show", "events"))
    parser.add_argument("run_id", help="Run identifier within the selected Session.")
    parser.add_argument("--session", required=True, help="Session that owns the Run.")
    parser.add_argument("--cwd", default=".", help="Workspace containing .pico/sessions.")
    return parser


def run_main(argv=None):
    args = build_run_parser().parse_args(argv)
    sessions = SessionStore(Workspace.build(args.cwd).root / ".pico" / "sessions")
    session = sessions.load(args.session)
    store = sessions.runs(session.id)
    if not store.has_events(args.run_id):
        raise SystemExit(f"Run Log not found for {args.run_id}")
    if args.command == "show":
        projection = store.load_run(args.run_id).projection
        print(json.dumps(projection.summary(), indent=2, sort_keys=True))
        return 0
    for entry in store.read_events(args.run_id):
        print(json.dumps(entry.to_dict(), sort_keys=True))
    return 0
