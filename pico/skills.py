"""Local Skill discovery, prompt indexing, and capability activation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path


SKILLS_DIR = ".pico/skills"
SKILL_FILENAME = "SKILL.md"
MAX_SKILL_FILE_BYTES = 20_000
MAX_SKILL_NAME_LENGTH = 64
MAX_SKILL_DESCRIPTION_LENGTH = 1_024

LIST_FIELDS = {"tools", "conflicts_with"}
BOOL_FIELDS = {"allowed_tools_strict", "enabled", "disable-model-invocation"}
INT_FIELDS = {"priority"}


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: str
    hash: str
    tools: tuple[str, ...] = ()
    allowed_tools_strict: bool = False
    priority: int = 0
    conflicts_with: tuple[str, ...] = ()
    when_to_use: str = ""
    when_not_to_use: str = ""
    version: str = ""
    enabled: bool = True
    disable_model_invocation: bool = False


def load_skill_catalog(root):
    """Load and validate local metadata without placing skill bodies in prompt context."""
    root = Path(root).resolve()
    skills_root = root / SKILLS_DIR
    if not skills_root.exists() or not skills_root.is_dir():
        return [], []

    skills = []
    diagnostics = []
    names = set()
    for path in sorted(skills_root.rglob(SKILL_FILENAME)):
        try:
            resolved = path.resolve()
            if os.path.commonpath([str(root), str(resolved)]) != str(root):
                continue
            if resolved.stat().st_size > MAX_SKILL_FILE_BYTES:
                continue
            raw = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            diagnostics.append(
                {"path": str(path), "level": "warning", "message": "could not read SKILL.md"}
            )
            continue
        parsed = parse_skill(raw)
        relative_path = resolved.relative_to(root).as_posix()
        if not parsed["enabled"]:
            continue
        name = parsed["name"] or resolved.parent.name
        errors = validate_skill_metadata(name, parsed["description"])
        if errors:
            diagnostics.extend(
                {"path": relative_path, "level": "warning", "message": error}
                for error in errors
            )
            continue
        if name in names:
            diagnostics.append(
                {
                    "path": relative_path,
                    "level": "warning",
                    "message": f"duplicate skill name '{name}' ignored",
                }
            )
            continue
        names.add(name)
        skills.append(
            SkillInfo(
                name=name,
                description=parsed["description"],
                path=relative_path,
                hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                tools=tuple(parsed["tools"]),
                allowed_tools_strict=parsed["allowed_tools_strict"],
                priority=parsed["priority"],
                conflicts_with=tuple(parsed["conflicts_with"]),
                when_to_use=parsed["when_to_use"],
                when_not_to_use=parsed["when_not_to_use"],
                version=parsed["version"],
                enabled=parsed["enabled"],
                disable_model_invocation=parsed["disable_model_invocation"],
            )
        )
    return skills, diagnostics


def load_skills(root):
    """Return valid project-local skills; use ``load_skill_catalog`` for diagnostics."""
    skills, _ = load_skill_catalog(root)
    return skills


def parse_skill(raw):
    text = str(raw or "").strip()
    metadata = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            metadata = _parse_frontmatter(parts[1])
    return {
        "name": str(metadata.get("name", "")).strip(),
        "description": str(metadata.get("description", "")).strip(),
        "tools": tuple(metadata.get("tools", ())),
        "allowed_tools_strict": bool(metadata.get("allowed_tools_strict", False)),
        "priority": int(metadata.get("priority", 0)),
        "conflicts_with": tuple(metadata.get("conflicts_with", ())),
        "when_to_use": str(metadata.get("when_to_use", "")).strip(),
        "when_not_to_use": str(metadata.get("when_not_to_use", "")).strip(),
        "version": str(metadata.get("version", "")).strip(),
        "enabled": bool(metadata.get("enabled", True)),
        "disable_model_invocation": bool(metadata.get("disable-model-invocation", False)),
    }


def validate_skill_metadata(name, description):
    errors = []
    name = str(name or "").strip()
    description = str(description or "").strip()
    if not name:
        errors.append("name is required")
    elif len(name) > MAX_SKILL_NAME_LENGTH:
        errors.append(f"name exceeds {MAX_SKILL_NAME_LENGTH} characters")
    elif not _valid_skill_name(name):
        errors.append("name must use lowercase letters, digits, and single hyphens")
    if not description:
        errors.append("description is required")
    elif len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
        errors.append(f"description exceeds {MAX_SKILL_DESCRIPTION_LENGTH} characters")
    return errors


def _valid_skill_name(name):
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return False
    return all(char.isdigit() or ("a" <= char <= "z") or char == "-" for char in name)


def _parse_frontmatter(text):
    metadata = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if key in LIST_FIELDS:
            metadata[key] = _parse_list(value)
        elif key in BOOL_FIELDS:
            metadata[key] = _parse_bool(value, default=(key == "enabled"))
        elif key in INT_FIELDS:
            metadata[key] = _parse_int(value)
        else:
            metadata[key] = value
    return metadata


def _parse_list(value):
    text = str(value or "").strip()
    if not text:
        return ()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return tuple(item.strip().strip('"').strip("'") for item in text.split(",") if item.strip())


def _parse_bool(value, default=False):
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1", "on"}:
        return True
    if text in {"false", "no", "0", "off"}:
        return False
    return bool(default)


def _parse_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def resolve_conflicts(skills):
    """Keep the highest-priority non-conflicting activated skills."""
    ordered = sorted(
        enumerate(list(skills or [])),
        key=lambda item: (-int(item[1].priority), item[0]),
    )
    selected = []
    selected_names = set()
    for _, skill in ordered:
        if skill.name in selected_names or set(skill.conflicts_with) & selected_names:
            continue
        selected.append(skill)
        selected_names.add(skill.name)
    return selected


def compute_active_tools(active_skills, all_tool_names):
    all_tool_names = {str(name) for name in all_tool_names}
    strict_skills = [
        skill for skill in active_skills or [] if skill.allowed_tools_strict
    ]
    if strict_skills:
        declared = {
            tool_name
            for skill in strict_skills
            for tool_name in skill.tools
        }
        return frozenset(declared & all_tool_names), True
    return None, False


def render_skill_index(skills):
    skills = [skill for skill in skills or [] if not skill.disable_model_invocation]
    if not skills:
        return ""
    lines = [
        "Available skills:",
        "- Read a listed SKILL.md with read_file only when the task clearly matches its description.",
        "- Resolve relative paths mentioned by a skill against that SKILL.md's parent directory.",
        "- Do not load a skill for unrelated work. After reading one, follow its instructions; strict skills may reduce the available tools.",
    ]
    for skill in skills:
        description = skill.description
        if skill.when_to_use:
            description = f"{description} Use when: {skill.when_to_use}".strip()
        if skill.when_not_to_use:
            description = (
                f"{description} Do not use when: {skill.when_not_to_use}"
            ).strip()
        lines.extend(
            [
                f"## {skill.name}",
                f"Description: {description}",
                f"Source: {skill.path}",
            ]
        )
    return "\n".join(lines)


def skill_metadata(available_skills, *, active_skills=(), rendered_text=""):
    available_skills = list(available_skills or [])
    active_skills = list(active_skills or [])
    return {
        "available_count": len(available_skills),
        "available_names": [skill.name for skill in available_skills],
        "available_paths": [skill.path for skill in available_skills],
        "manual_only_names": [skill.name for skill in available_skills if skill.disable_model_invocation],
        "active_count": len(active_skills),
        "active_names": [skill.name for skill in active_skills],
        "active_paths": [skill.path for skill in active_skills],
        "active_strict": [bool(skill.allowed_tools_strict) for skill in active_skills],
        "rendered_chars": len(str(rendered_text or "")),
    }
