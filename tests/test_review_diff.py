import json
import subprocess

import pytest

from pico import FakeModelClient, ModelAction, Pico, SessionStore, WorkspaceContext
from pico.review import (
    REVIEW_ALLOWED_TOOLS,
    Finding,
    GitDiffError,
    ReviewReport,
    load_git_diff,
    merge_review_reports,
    parse_added_lines,
    render_review_markdown,
)
from pico.review.cli import build_review_parser, run_review


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def build_repository(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "pico@example.invalid")
    git(tmp_path, "config", "user.name", "Pico Test")
    source = tmp_path / "src"
    source.mkdir()
    (source / "service.py").write_text(
        "def value():\n    return 'old'\n", encoding="utf-8"
    )
    git(tmp_path, "add", "src/service.py")
    git(tmp_path, "commit", "-m", "base")
    base = git(tmp_path, "rev-parse", "HEAD")

    (source / "service.py").write_text(
        "def value():\n    return 'new-value-with-context-' + 'x' * 80\n",
        encoding="utf-8",
    )
    (source / "helper.py").write_text(
        "def helper():\n    return 'helper-value-' + 'y' * 80\n",
        encoding="utf-8",
    )
    git(tmp_path, "add", "src/service.py", "src/helper.py")
    git(tmp_path, "commit", "-m", "head")
    return base, git(tmp_path, "rev-parse", "HEAD")


def test_load_git_diff_resolves_clean_head_and_added_lines(tmp_path):
    base, head = build_repository(tmp_path)

    loaded = load_git_diff(tmp_path, base, head)

    assert loaded.base_sha == base
    assert loaded.head_sha == head
    assert set(loaded.changed_files) == {"src/helper.py", "src/service.py"}
    assert 2 in loaded.changed_lines()["src/service.py"]
    assert loaded.changed_lines()["src/helper.py"] == frozenset({1, 2})
    requests = loaded.requests(max_chars=300)
    assert len(requests) == 2
    assert {request.changed_files[0] for request in requests} == set(loaded.changed_files)


def test_load_git_diff_rejects_dirty_or_wrong_checkout(tmp_path):
    base, head = build_repository(tmp_path)
    (tmp_path / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(GitDiffError, match="clean"):
        load_git_diff(tmp_path, base, head)

    (tmp_path / "untracked.txt").unlink()
    git(tmp_path, "checkout", base)
    with pytest.raises(GitDiffError, match="HEAD"):
        load_git_diff(tmp_path, base, head)


def test_parse_rename_only_diff_uses_destination_path():
    text = (
        "diff --git a/src/old.py b/src/new.py\n"
        "similarity index 100%\n"
        "rename from src/old.py\n"
        "rename to src/new.py\n"
    )

    assert parse_added_lines(text) == {"src/new.py": frozenset()}


def finding():
    return Finding(
        finding_id="finding_0123456789abcdef",
        category="correctness",
        severity="high",
        confidence=0.9,
        path="src/service.py",
        start_line=2,
        end_line=2,
        title="Incorrect return value",
        explanation="The changed value violates the caller contract.",
        evidence="The added return expression changes the public result.",
        suggested_fix="Restore the expected value.",
    )


def test_merge_and_render_review_reports():
    first = ReviewReport(
        review_id="review_one",
        run_ids=["run_1"],
        policy_version="policy-v1",
        policy_digest="sha256:abc",
        verdict="findings",
        summary="one",
        findings=[finding()],
    )
    second = first.model_copy(update={"run_ids": ["run_2"]})

    merged = merge_review_reports([first, second])
    markdown = render_review_markdown(merged)

    assert merged.run_ids == ["run_1", "run_2"]
    assert len(merged.findings) == 1
    assert "src/service.py:2" in markdown
    assert "Incorrect return value" in markdown


def test_review_cli_pipeline_writes_json_report(tmp_path):
    base, head = build_repository(tmp_path)
    output = tmp_path / "review.json"
    args = build_review_parser().parse_args(
        [
            "--repo",
            str(tmp_path),
            "--base",
            base,
            "--head",
            head,
            "--format",
            "json",
            "--output",
            str(output),
        ]
    )

    def agent_factory(_args, root):
        response = json.dumps(
            {"verdict": "clean", "summary": "No supported defect.", "findings": []}
        )
        return Pico(
            FakeModelClient([ModelAction.final(response)]),
            WorkspaceContext.build(root, repo_root_override=root),
            SessionStore(root / ".pico" / "review-test-sessions"),
            approval_policy="never",
            read_only=True,
            allowed_tools=REVIEW_ALLOWED_TOOLS,
            verification_command="",
        )

    report = run_review(args, agent_factory=agent_factory)

    assert report.verdict == "clean"
    assert report.run_ids
    assert json.loads(output.read_text(encoding="utf-8"))["verdict"] == "clean"
