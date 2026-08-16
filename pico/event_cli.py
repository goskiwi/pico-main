"""Replay and summarize hash-validated Runtime event logs."""

import argparse
import json
from pathlib import Path

from .run_store import RunStore


def build_event_parser():
    parser = argparse.ArgumentParser(prog="pico events")
    parser.add_argument("command", choices=("replay", "stats"))
    parser.add_argument("run_id", help="Run identifier under .pico/runs.")
    parser.add_argument("--cwd", default=".", help="Workspace containing .pico/runs.")
    return parser


def event_main(argv=None):
    args = build_event_parser().parse_args(argv)
    store = RunStore(Path(args.cwd).resolve() / ".pico" / "runs")
    events = store.read_events(args.run_id)
    if not events:
        raise SystemExit(f"Runtime event log not found for {args.run_id}")
    projection = store.replay(args.run_id)
    if args.command == "stats":
        print(json.dumps(projection.summary(), indent=2, sort_keys=True))
        return 0
    for event in events:
        print(json.dumps(event, sort_keys=True))
    return 0
