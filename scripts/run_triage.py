#!/usr/bin/env python3
"""Run Pico Triage against one JSON case."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from applications.triage import TriageCase, TriageWorkflow
from pico import OpenAICompatibleModelClient, PicoConfig
from pico.config import load_project_env, provider_env


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("case", type=Path, help="Triage case JSON file.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask")
    parser.add_argument("--max-tool-executions", type=int, default=40)
    args = parser.parse_args(argv)

    case = TriageCase.from_json(args.case)
    load_project_env(case.repository_root, boundary=case.repository_root)
    model = args.model or provider_env("PICO_OPENAI_MODEL", "gpt-5.4")
    base_url = args.base_url or provider_env(
        "PICO_OPENAI_API_BASE", "https://api.openai.com/v1"
    )
    api_key = provider_env("PICO_OPENAI_API_KEY")

    def model_client():
        return OpenAICompatibleModelClient(
            model,
            base_url,
            api_key,
            args.temperature,
            args.timeout,
        )

    report = TriageWorkflow(
        model_client(),
        config=PicoConfig(
            approval_policy=args.approval,
            max_tool_executions=args.max_tool_executions,
        ),
        subagent_model_client_factory=lambda _spec: model_client(),
    ).run(case)
    rendered = json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
