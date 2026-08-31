import pytest

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.run_lifecycle import RunLifecycle
from pico.task_classifier import (
    StaticTaskIntentClassifier,
    TaskIntentClassifier,
)


class ClassifierProvider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.created_clients = 0

    def new_isolated_client(self):
        self.created_clients += 1
        return FakeModelClient([self.outputs.pop(0)])


def test_task_classifier_retries_one_invalid_structured_result():
    provider = ClassifierProvider(
        [
            ModelAction.final("not a classification"),
            ModelAction.tool(
                "classify_task_intent",
                {"intent": "modify_optional"},
            ),
        ]
    )

    intent = TaskIntentClassifier(provider).classify("Review and fix if needed")

    assert intent == "modify_optional"
    assert provider.created_clients == 2


def test_task_classifier_fails_before_runtime_tools_after_two_invalid_results():
    provider = ClassifierProvider(
        [ModelAction.final("bad"), ModelAction.final("still bad")]
    )

    with pytest.raises(RuntimeError, match="classify_task_intent"):
        TaskIntentClassifier(provider).classify("Do something")


def test_public_ask_stops_before_run_when_classifier_fails(tmp_path):
    client = FakeModelClient([ModelAction.final("must remain unused")])

    class FailingClassifier:
        @staticmethod
        def classify(_message):
            raise RuntimeError("classification unavailable")

    agent = Pico(
        client,
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto"),
        task_classifier=FailingClassifier(),
    )

    with pytest.raises(RuntimeError, match="classification unavailable"):
        agent.ask("Fix it")

    assert agent.run.task is None
    assert len(client.outputs) == 1


def test_runtime_derives_contract_from_internal_intent(tmp_path):
    agent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="python -m pytest -q",
        ),
        task_classifier=StaticTaskIntentClassifier("read_only"),
    )
    lifecycle = RunLifecycle(agent)

    read_only = lifecycle._task_contract("Inspect", intent="read_only")
    modify = lifecycle._task_contract("Fix", intent="modify")
    optional = lifecycle._task_contract("Review", intent="modify_optional")

    assert read_only.to_dict() == {
        "goal": "Inspect",
        "task_kind": "read_only",
        "requires_workspace_change": False,
        "requires_verification": False,
        "allowed_write_paths": None,
    }
    assert modify.requires_workspace_change is True
    assert modify.requires_verification is True
    assert optional.requires_workspace_change is False
    assert optional.requires_verification is True
