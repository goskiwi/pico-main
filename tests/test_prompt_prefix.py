from pico.prompt_prefix import build_prompt_prefix
from pico.tools import (
    build_action_tools,
    build_tool_registry,
)
from pico.workspace import WorkspaceContext


class _Agent:
    def __init__(self, root):
        self.root = root


def test_build_prompt_prefix_renders_tools_and_workspace_metadata(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))

    prefix = build_prompt_prefix(workspace=workspace, tools=tools)

    assert "You are pico" in prefix.text
    assert "Tools:" in prefix.text
    assert "- read_file [safe]" in prefix.text
    assert "<tool>" not in prefix.text
    assert "Workspace:" in prefix.text
    assert prefix.content_hash


def test_model_workspace_panel_never_exposes_host_absolute_paths(tmp_path):
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)
    workspace = WorkspaceContext.build(nested, repo_root_override=tmp_path)

    text = workspace.text()

    assert str(tmp_path) not in text
    assert "- cwd: src/package" in text
    assert "- repo_root: ." in text
    assert "- shell_cwd: /workspace" in text


def test_native_function_schemas_are_openai_strict(tmp_path):
    definitions = build_action_tools(build_tool_registry(_Agent(tmp_path)))
    for definition in definitions:
        schema = definition["parameters"]
        assert definition["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
