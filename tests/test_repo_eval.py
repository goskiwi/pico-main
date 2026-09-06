"""Exercise experiment integrity without paid models or external repositories."""
import json
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pico import FakeModelClient, ModelAction, Pico, PicoConfig, SessionStore, Workspace
from pico.command_runner import CommandRunner
from pico.mutations import content_revision
from scripts.repo_eval_tasks import command
from scripts.repo_eval_verify import capture_patch
from scripts.run_repo_eval import (
    MeteredClient,
    error_message,
    forbidden_change,
    run_trial,
    summarize,
    usage_totals,
)


def test_evaluation_error_keeps_multiline_detail_and_subprocess_stderr():
    class Client:
        api_key = "secret"

    multiline = RuntimeError("public tests failed:\nAssertionError: secret value")
    assert error_message(multiline, Client()) == (
        "public tests failed:\nAssertionError: <redacted> value"
    )

    process = subprocess.CalledProcessError(
        2, ["docker", "inspect"], stderr="daemon unavailable: secret"
    )
    detail = error_message(process, Client())
    assert "returned non-zero exit status 2" in detail
    assert "daemon unavailable: <redacted>" in detail


def test_repo_map_switch_changes_prompt_and_skips_indexing(tmp_path, monkeypatch):
    (tmp_path / "payment.py").write_text("def retry_payment():\n    return 1\n")
    model = FakeModelClient([
        ModelAction.tool("read_file", {"path": "payment.py"}),
        ModelAction.final("Inspected payment retries."),
    ])
    agent = Pico(model, Workspace.build(tmp_path),
                 SessionStore(tmp_path / ".pico/sessions").create(tmp_path),
                 config=PicoConfig(mode="ask"))
    agent.ask("Explain retry_payment")
    on_prompt, _ = agent.prompt.build("Explain retry_payment")
    assert '<section name="repo_map">' in on_prompt.input_text
    agent.config = replace(agent.config, repo_map_enabled=False)

    def unexpected_index(*args, **kwargs):
        raise AssertionError("disabled RepoMap must not scan the repository")

    monkeypatch.setattr(agent.dependencies.repo_map, "render", unexpected_index)
    off_prompt, _ = agent.prompt.build("Explain retry_payment")
    assert '<section name="repo_map">' not in off_prompt.input_text
    assert on_prompt.instructions == off_prompt.instructions
    assert '"Explain retry_payment"' in off_prompt.input_text


def test_usage_includes_compaction_and_never_treats_missing_as_zero():
    records = [
        {"purpose": "agent", "input_tokens": 100, "output_tokens": 20},
        {"purpose": "compaction", "input_tokens": 40, "output_tokens": 10},
    ]
    assert usage_totals(records)["total_tokens"] == 170
    incomplete = usage_totals(records + [{"purpose": "agent", "error": "Timeout"}])
    assert incomplete["total_tokens"] is None
    assert incomplete["reported_input_tokens"] == 140
    assert incomplete["missing_input_tokens_requests"] == 1
    assert incomplete["compaction_requests"] == 1
    assert usage_totals([])["total_tokens"] is None


def test_provider_total_disagreement_remains_visible():
    totals = usage_totals([{
        "purpose": "agent", "input_tokens": 14344, "output_tokens": 154,
        "total_tokens": 14499,
    }])
    assert totals["total_tokens"] == 14498
    assert totals["provider_total_tokens"] == 14499
    assert totals["provider_total_delta"] == 1


def test_metered_isolated_client_keeps_summary_cost_and_failed_requests(monkeypatch):
    from pico.providers.clients import OpenAICompatibleModelClient

    def response(self, *args, **kwargs):
        if kwargs.get("fail"):
            raise TimeoutError("request timed out: secret")
        self.last_completion_metadata = {"input_tokens": 12, "output_tokens": 3}
        return ModelAction.final("done")

    monkeypatch.setattr(OpenAICompatibleModelClient, "complete_action", response)
    client = MeteredClient("test", "https://example.test/v1", "secret", 0, 10)
    client.complete_action("main", 10)
    summary_client = client.new_isolated_client()
    summary_client.complete_action("summary", 10)
    assert usage_totals(client.measurements)["total_tokens"] == 30
    with pytest.raises(TimeoutError):
        client.complete_action("failed", 10, fail=True)
    assert client.measurements[-1].get("input_tokens") is None
    assert client.measurements[-1]["error_message"] == "request timed out: <redacted>"
    assert usage_totals(client.measurements)["compaction_requests"] == 1
    assert usage_totals(client.measurements)["total_tokens"] is None


def row(task, variant):
    return {"trial_id": f"{task}_{variant}", "instance_id": task, "repeat": 1,
            "variant": variant, "scope_violations": [], "runtime_status": "completed",
            "total_tokens": 100, "inference_seconds": 1, "human_interventions": 0,
            "generation_error": None, "patch_nonempty": True, "stop_reason": None}


def test_scoring_distinguishes_infrastructure_missing_pairs_and_test_cheating():
    rows = [row(task, variant) for task in ("a", "b")
            for variant in ("repomap_on", "repomap_off")]
    rows[1]["scope_violations"] = ["tests/test_bug.py"]
    judgments = [
        {"trial_id": "a_repomap_on", "resolved": True},
        {"trial_id": "a_repomap_off", "resolved": True},
        {"trial_id": "b_repomap_on", "resolved": False,
         "infrastructure_error": "evaluation_timeout"},
    ]
    merged, summary = summarize(rows, judgments)
    assert summary["repomap_on"]["attempted"] == 2
    assert summary["repomap_on"]["scored"] == 1
    assert summary["repomap_on"]["resolved"] == 1
    assert summary["paired"]["scored_pairs"] == 1
    assert summary["paired"]["on_only_resolved"] == 1
    assert merged[1]["failure_reason"] == "forbidden_test_or_config_change"
    assert merged[2]["resolved"] is None
    assert merged[3]["failure_reason"] == "not_judged"


def test_candidate_test_timeout_is_a_failed_attempt_in_a_valid_environment():
    sample = row("a", "repomap_on")
    merged, summary = summarize([sample], [{
        "trial_id": sample["trial_id"], "resolved": False,
        "evaluation_error": "evaluation_timeout", "infrastructure_error": None,
    }])
    assert summary["repomap_on"]["scored"] == 1
    assert summary["repomap_on"]["success_rate_scored"] == 0
    assert merged[0]["failure_reason"] == "evaluation_timeout"


def test_setup_failure_is_persisted_without_calling_the_model(tmp_path):
    model = FakeModelClient([])
    model.measurements = []
    model.model = "scripted-fixture"
    result, _ = run_trial(
        {"instance_id": "missing"}, tmp_path / "absent", tmp_path / "trials",
        model, PicoConfig(mode="auto"), 1, "repomap_off",
    )
    assert result["setup_error"] == "CalledProcessError"
    assert result["provider_requests"] == 0
    assert (tmp_path / "trials" / result["trial_id"] / "result.json").is_file()
    merged, summary = summarize([result], [])
    assert summary["repomap_off"]["attempted"] == 1
    assert summary["repomap_off"]["unscored"] == 1
    assert merged[0]["failure_reason"].startswith("trial_setup:")


@pytest.mark.parametrize("path", ["tests/test_api.py", "pkg/tests/helper.py", "conftest.py",
                                  "pytest.ini", "requirements-dev.txt", "pyproject.toml"])
def test_test_or_environment_changes_cannot_count_as_success(path):
    assert forbidden_change(path)
    assert not forbidden_change("src/payment.py")


def test_scripted_trial_produces_patch_that_passes_real_independent_tests(tmp_path):
    prepared = tmp_path / "prepared"
    source = prepared / "source"
    source.mkdir(parents=True)
    original = "def add(a, b):\n    return a - b\n"
    (source / "calculator.py").write_text(original)
    (source / "test_calculator.py").write_text(
        "from calculator import add\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    command(["git", "init"], cwd=source)
    command(["git", "add", "."], cwd=source)
    command(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Test",
             "-c", "user.email=test@example.test", "commit", "-m", "fixture"], cwd=source)
    (prepared / "problem.txt").write_text("Addition returns incorrect results.")
    before = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q"], cwd=source,
                            capture_output=True, check=False, timeout=30)
    assert before.returncode == 1
    model = FakeModelClient([
        ModelAction.tool("read_file", {"path": "calculator.py"}),
        ModelAction.tool("edit_file", {
            "path": "calculator.py", "old_text": "return a - b",
            "new_text": "return 0",
            "expected_revision": content_revision(original.encode()),
        }),
        ModelAction.final("First attempt, request public verification."),
        ModelAction.tool("edit_file", {
            "path": "calculator.py", "old_text": "return 0",
            "new_text": "return a + b",
            "expected_revision": content_revision(original.replace("return a - b", "return 0").encode()),
        }),
        ModelAction.final("Fixed addition."),
    ])
    model.measurements = []
    model.model = "scripted-fixture"
    result, prediction = run_trial(
        {"instance_id": "fixture", "public_verification":
         f'{shlex.quote(sys.executable)} -B -c "from calculator import add; assert add(2,0) == 2"'},
        prepared, tmp_path / "trials", model,
        PicoConfig(mode="auto"), 1, "repomap_on",
        command_runner_factory=CommandRunner,
    )
    assert result["runtime_status"] == "completed"
    assert result["public_verification_results"] == ["failed", "passed"]
    assert result["total_tokens"] is None
    assert result["scope_violations"] == []
    assert "+    return a + b" in prediction["model_patch"]
    candidate = tmp_path / "trials" / result["trial_id"] / "candidate.patch"
    command(["git", "apply", candidate], cwd=source)
    after = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q"], cwd=source,
                           capture_output=True, check=False, timeout=30)
    assert after.returncode == 0
    persisted = json.loads(candidate.with_name("result.json").read_text())
    assert persisted["human_interventions"] == 0
    assert not (Path(candidate.parent) / "workspace/instance.json").exists()


def test_public_verifier_patch_capture_keeps_real_index_and_includes_new_files(tmp_path):
    command(["git", "init"], cwd=tmp_path)
    (tmp_path / "module.py").write_text("value = 1\n")
    command(["git", "add", "."], cwd=tmp_path)
    command(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Test",
             "-c", "user.email=test@example.test", "commit", "-m", "fixture"], cwd=tmp_path)
    (tmp_path / "module.py").write_text("value = 2\n")
    command(["git", "add", "module.py"], cwd=tmp_path)
    (tmp_path / "module.py").write_text("value = 3\n")
    (tmp_path / "new.py").write_text("new_value = 4\n")
    (tmp_path / ".pico").mkdir()
    (tmp_path / ".pico/events.jsonl").write_text("private runtime state\n")
    index = (tmp_path / ".git/index").read_bytes()
    patch = capture_patch(tmp_path).decode()
    assert (tmp_path / ".git/index").read_bytes() == index
    assert "+value = 3" in patch
    assert "+new_value = 4" in patch
    assert ".pico" not in patch
