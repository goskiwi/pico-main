import re
from pathlib import Path


def test_readme_documents_precedence_and_environment_mapping():
    text = Path("README.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert re.search(r"^#{1,6}\s+.*precedence", text, flags=re.IGNORECASE | re.MULTILINE)
    for term in ("default", "file", "environment", "app_"):
        assert term in lowered
    assert lowered.index("default") < lowered.index("file") < lowered.index("environment")


def test_readme_contains_an_executable_python_example():
    text = Path("README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```python\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    assert blocks, "add a fenced Python example"
    executed = False
    for block in blocks:
        if "load_config" in block and "assert" in block and "9000" in block:
            exec(compile(block, "README.md", "exec"), {})
            executed = True
    assert executed, "example must call load_config and assert the environment override"
