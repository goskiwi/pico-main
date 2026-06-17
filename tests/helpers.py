from pico import FakeModelClient, MiniAgent, WorkspaceContext
from pico.session_store import SessionStore


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    feature_flags = {"llm_memory_extract": False, "llm_history_compaction": False}
    feature_flags.update(kwargs.pop("feature_flags", {}) or {})
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        feature_flags=feature_flags,
        **kwargs,
    )
