"""Show a Run summary or its persisted events."""

import argparse
import json
from pathlib import Path

from .run_store import RunStore


def build_run_parser():
    parser = argparse.ArgumentParser(prog="pico run")
    parser.add_argument("command", choices=("show", "events"))
    parser.add_argument("run_id", help="Run identifier under .pico/runs.")
    parser.add_argument("--cwd", default=".", help="Workspace containing .pico/runs.")
    return parser


def run_main(argv=None):
    args = build_run_parser().parse_args(argv)
    store = RunStore(Path(args.cwd).resolve() / ".pico" / "runs")
    events = store.read_events(args.run_id)
    if not events:
        raise SystemExit(f"Run Log not found for {args.run_id}")
    if args.command == "show":
        print(json.dumps(store.replay(args.run_id).summary(), indent=2, sort_keys=True))
        return 0
    for entry in events:
        print(json.dumps(entry.to_dict(), sort_keys=True))
    return 0
