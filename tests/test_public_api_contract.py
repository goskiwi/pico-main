import pytest

from pico import (
    FakeModelClient,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
    build_agent,
    build_arg_parser,
    build_welcome,
    main,
)


def test_supported_public_api_is_importable():
    assert Pico is not None
    assert PicoConfig is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert callable(build_agent)
    assert callable(build_arg_parser)
    assert callable(build_welcome)
    assert callable(main)


def test_build_agent_returns_pico(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])

    agent = build_agent(args)

    assert isinstance(agent, Pico)


def test_flat_runtime_configuration_is_rejected(tmp_path):
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        Pico(
            FakeModelClient([]),
            WorkspaceContext.build(tmp_path),
            SessionStore(tmp_path / ".pico" / "sessions"),
            approval_policy="auto",
        )


def test_feature_modules_are_importable_from_package_paths():
    from pico.evaluation.evaluator import BenchmarkEvaluator
    from pico.evaluation.metrics import run_context_governance_ablation
    from pico.features.memory import SessionWorkingMemory
    from pico.project_memory import ProjectMemoryStore
    from pico.providers.clients import FakeModelClient as ProviderFakeModelClient

    assert BenchmarkEvaluator is not None
    assert SessionWorkingMemory is not None
    assert ProjectMemoryStore is not None
    assert ProviderFakeModelClient is not None
    assert callable(run_context_governance_ablation)
