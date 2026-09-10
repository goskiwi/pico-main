"""Command-line composition and presentation."""

import argparse
import json
import shlex
import sys
from pathlib import Path

from .config import load_project_env, provider_env
from .providers.clients import DEFAULT_OPENAI_BASE_URL, OpenAICompatibleModelClient
from .runtime import Pico
from .runtime_config import PicoConfig
from .session_store import SessionStore
from .trace import TracePrinter
from .workspace import Workspace

DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_SECRET_ENV_NAMES = (
    "PICO_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "PICO_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)


def detect_verification_command(repo_root):
    tests = Path(repo_root).resolve() / "tests"
    if tests.is_dir() and not tests.is_symlink() and any(
        path.is_file() and not path.is_symlink() for path in tests.rglob("test_*.py")
    ):
        return f"{shlex.quote(sys.executable)} -m pytest -q"
    return ""


def _terminal_approval(name, args, plan):
    request = {
        "arguments": args,
        "targets": [str(target) for _logical, target in plan.paths],
        "operation": plan.operation or name,
    }
    try:
        answer = input(
            f"approve {name} {json.dumps(request, ensure_ascii=True)}? [y/N] "
        )
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _model_client(args):
    model = args.model or provider_env("PICO_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    configured_base = args.base_url or provider_env("PICO_OPENAI_API_BASE")
    api_key = provider_env("PICO_OPENAI_API_KEY")
    if not api_key and not configured_base:
        raise RuntimeError(
            "Set PICO_OPENAI_API_KEY or pass --base-url for an intentional no-auth endpoint."
        )
    return OpenAICompatibleModelClient(
        model=model,
        base_url=configured_base or DEFAULT_OPENAI_BASE_URL,
        api_key=api_key,
        temperature=args.temperature,
        timeout=args.openai_timeout,
    )


def build_agent(args):
    workspace = Workspace.build(args.cwd)
    load_project_env(workspace.root, boundary=workspace.root)
    secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    secret_names.update(name.upper() for name in args.secret_env_names)
    config = PicoConfig(
        mode=args.mode,
        max_agent_turns=args.max_agent_turns,
        max_parallel_tools=args.max_parallel_tools,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=frozenset(secret_names),
        allowed_tools=tuple(args.allowed_tools) if args.allowed_tools else None,
        turn_timeout_seconds=args.turn_timeout,
        provider_context_limit_tokens=args.provider_context_limit,
        compaction_reserve_tokens=args.compaction_reserve_tokens,
        compaction_keep_recent_tokens=args.compaction_keep_recent_tokens,
        summary_max_output_tokens=args.summary_max_output_tokens,
        verification_command=(
            args.verify_command.strip() or detect_verification_command(workspace.root)
        ),
        allowed_write_paths=(
            tuple(args.allowed_write_paths) if args.allowed_write_paths else None
        ),
        memory_enabled=not args.no_memory,
    )
    store = SessionStore(workspace.root / ".pico" / "sessions")
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest_active()
        if not session_id:
            raise ValueError("no unfinished Session is available")
    constructor = Pico.resume if session_id else Pico.create
    session_args = (
        {"session": store.load(session_id, workspace.root)}
        if session_id
        else {"session_store": store}
    )
    return constructor(
        _model_client(args),
        workspace,
        config=config,
        trace=TracePrinter(sys.stderr) if args.trace else None,
        approval_handler=_terminal_approval,
        **session_args,
    )


def build_arg_parser():
    defaults = PicoConfig()
    parser = argparse.ArgumentParser(
        description="A readable local coding-agent runtime.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--resume", help="Session id or latest")
    parser.add_argument("--mode", choices=("ask", "code", "auto"), default=defaults.mode)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--openai-timeout", type=int, default=300)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--max-agent-turns", type=int, default=defaults.max_agent_turns)
    parser.add_argument("--max-parallel-tools", type=int, default=defaults.max_parallel_tools)
    parser.add_argument("--max-new-tokens", type=int, default=defaults.max_new_tokens)
    parser.add_argument("--turn-timeout", type=int, default=defaults.turn_timeout_seconds)
    parser.add_argument(
        "--provider-context-limit",
        type=int,
        default=defaults.provider_context_limit_tokens,
    )
    parser.add_argument(
        "--compaction-reserve-tokens",
        type=int,
        default=defaults.compaction_reserve_tokens,
    )
    parser.add_argument(
        "--compaction-keep-recent-tokens",
        type=int,
        default=defaults.compaction_keep_recent_tokens,
    )
    parser.add_argument(
        "--summary-max-output-tokens",
        type=int,
        default=defaults.summary_max_output_tokens,
    )
    parser.add_argument("--verify-command", default="")
    parser.add_argument("--allow-tool", dest="allowed_tools", action="append", default=[])
    parser.add_argument(
        "--allow-write", dest="allowed_write_paths", action="append", default=[]
    )
    parser.add_argument(
        "--secret-env-name", dest="secret_env_names", action="append", default=[]
    )
    parser.add_argument("--no-memory", action="store_true")
    return parser


def _print_outcome(outcome):
    print(f"Status: {outcome.status}")
    print(f"Verification: {outcome.verification}")
    print(f"Session: {outcome.session_id}")
    if outcome.stop_reason:
        print(f"Stop reason: {outcome.stop_reason}")
    print()
    print(outcome.answer)


def main(argv=None):
    args = build_arg_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        agent = build_agent(args)
    except (RuntimeError, ValueError) as exc:
        print(f"pico: {exc}", file=sys.stderr)
        return 2
    print(f"Pico | {agent.config.mode} | {agent.workspace.root} | {agent.session.id}")
    if args.prompt:
        outcome = agent.ask(" ".join(args.prompt).strip())
        _print_outcome(outcome)
        return 0 if outcome.status == "completed" else 1
    while True:
        try:
            value = input("\npico> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not value:
            continue
        if value in {"/exit", "/quit"}:
            return 0
        if value == "/session":
            print(agent.session.path)
            continue
        if value == "/state":
            print(json.dumps(agent.session.run, indent=2, ensure_ascii=False))
            continue
        if value == "/reset":
            agent.reset()
            print("session reset")
            continue
        _print_outcome(agent.ask(value))
