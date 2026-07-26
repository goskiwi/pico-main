from pico.workspace import WorkspaceContext


def test_workspace_uses_only_an_explicit_verification_command(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "__init__.py").write_text("", encoding="utf-8")

    workspace = WorkspaceContext.build(
        tmp_path,
        repo_root_override=tmp_path,
        verification_command="PYTHONPATH=. python -m pytest -q",
    )

    assert workspace.verification_command == "PYTHONPATH=. python -m pytest -q"
    assert "verification_command: PYTHONPATH=. python -m pytest -q" in workspace.text()


def test_workspace_does_not_guess_a_verification_command(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    workspace = WorkspaceContext.build(tmp_path, repo_root_override=tmp_path)

    assert workspace.verification_command == ""
    assert "verification_command: - none" in workspace.text()
