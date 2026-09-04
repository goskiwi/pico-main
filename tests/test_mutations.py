import pytest

from pico.mutations import RevisionConflict, WorkspaceMutationService, file_revision


def test_large_text_creation_and_edit_have_no_file_size_gate(tmp_path):
    target = tmp_path / "source.txt"
    content = "first\n" + "context\n" * 10 + "x" * 3_000_000 + "\n"
    service = WorkspaceMutationService(tmp_path)

    created = service.write(target, content)
    edited = service.edit(target, "first", "更新后的内容", created.after_revision)

    assert created.changed and edited.changed
    assert target.read_text() == content.replace("first", "更新后的内容")
    assert file_revision(target) == edited.after_revision


def test_large_external_change_still_causes_revision_conflict(tmp_path):
    target = tmp_path / "source.txt"
    target.write_text("before\n")
    revision = file_revision(target)
    content = "external\n" + "x" * 3_000_000
    target.write_text(content)

    with pytest.raises(RevisionConflict):
        WorkspaceMutationService(tmp_path).edit(target, "before", "after", revision)

    assert target.read_text() == content
