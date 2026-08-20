"""Replay and summarize a Run Journal."""

import argparse
import json
from pathlib import Path

from .run_journal import replay_entries
from .run_store import RunStore


def build_journal_parser():
    parser = argparse.ArgumentParser(prog="pico journal")
    parser.add_argument("command", choices=("replay", "stats"))
    parser.add_argument("run_id", help="Run identifier under .pico/runs.")
    parser.add_argument("--cwd", default=".", help="Workspace containing .pico/runs.")
    return parser


def journal_main(argv=None):
    args = build_journal_parser().parse_args(argv)
    store = RunStore(Path(args.cwd).resolve() / ".pico" / "runs")
    entries = store.read_entries(args.run_id)
    if not entries:
        raise SystemExit(f"Run Journal not found for {args.run_id}")
    if args.command == "stats":
        print(json.dumps(replay_entries(entries).summary(), indent=2, sort_keys=True))
        return 0
    for entry in entries:
        print(json.dumps(entry.to_dict(), sort_keys=True))
    return 0
