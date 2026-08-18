"""Command-line entry point for local Git revision review."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from ..cli import (
    DEFAULT_SECRET_ENV_NAMES,
    _build_model_client,
    _configured_secret_names,
)
from ..config import load_env_file
from ..runtime import Pico
from ..session_store import SessionStore
from ..workspace import WorkspaceContext
from .contracts import MAX_INLINE_DIFF_CHARS
from .diff import GitDiffError, load_git_diff
from .report import merge_review_reports, render_review_json, render_review_markdown
from .reviewer import REVIEW_ALLOWED_TOOLS, PRReviewer


def build_review_parser():
    parser = argparse.ArgumentParser(
        prog="pico-review",
        description="Review one clean local Git base/head diff with a read-only Pico Runtime.",
    )
    parser.add_argument("--repo", default=".", help="Clean repository checked out at head.")
    parser.add_argument("--base", required=True, help="Base Git revision.")
    parser.add_argument("--head", required=True, help="Head Git revision; must equal repository HEAD.")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", type=Path, help="Output path; stdout when omitted.")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--openai-timeout", type=int, default=300)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--run-timeout", type=int, default=600)
    parser.add_argument("--provider-context-limit", type=int, default=64000)
    parser.add_argument("--sandbox-image", default="pico/sandbox:latest")
    parser.add_argument(
        "--diff-chunk-chars", type=int, default=MAX_INLINE_DIFF_CHARS
    )
    parser.add_argument(
        "--secret-env-name", dest="secret_env_names", action="append", default=[]
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Explicit trusted provider env file; target-repository env files are not auto-loaded.",
    )
    return parser


def build_review_agent(args, root):
    if args.env_file:
        load_env_file(args.env_file)
    workspace = WorkspaceContext.build(root, repo_root_override=root)
    secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    secret_names.update(_configured_secret_names(args))
    return Pico(
        model_client=_build_model_client(args),
        workspace=workspace,
        session_store=SessionStore(Path(root) / ".pico" / "review-sessions"),
        approval_policy="never",
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        read_only=True,
        secret_env_names=sorted(secret_names),
        allowed_tools=sorted(REVIEW_ALLOWED_TOOLS),
        run_timeout_seconds=args.run_timeout,
        provider_context_limit_tokens=args.provider_context_limit,
        sandbox_image=args.sandbox_image,
        verification_command="",
    )


def run_review(args, *, agent_factory=build_review_agent):
    loaded = load_git_diff(args.repo, args.base, args.head)
    reports = []
    for request in loaded.requests(max_chars=args.diff_chunk_chars):
        reviewer = PRReviewer(agent_factory(args, loaded.root))
        reports.append(reviewer.review(request))
    report = merge_review_reports(reports)
    rendered = (
        render_review_json(report)
        if args.format == "json"
        else render_review_markdown(report)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return report


def main(argv=None):
    args = build_review_parser().parse_args(argv)
    try:
        report = run_review(args)
    except (GitDiffError, ValidationError, RuntimeError, ValueError) as exc:
        print(f"pico-review: {exc}", file=sys.stderr)
        return 2
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
