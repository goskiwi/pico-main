"""Local skill loading, model selection, and prompt rendering."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re


SKILLS_DIR = ".pico/skills"
SKILL_FILENAME = "SKILL.md"
DEFAULT_SKILL_LIMIT = 3
MAX_SKILL_FILE_BYTES = 20_000
SKILL_SELECTOR_MAX_TOKENS = 200

LIST_FIELDS = {"tools", "trigger_keywords", "conflicts_with"}
BOOL_FIELDS = {"allowed_tools_strict", "enabled"}
INT_FIELDS = {"priority"}


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    content: str
    path: str
    hash: str
    tools: tuple[str, ...] = ()
    allowed_tools_strict: bool = False
    trigger_keywords: tuple[str, ...] = ()
    priority: int = 0
    conflicts_with: tuple[str, ...] = ()
    when_to_use: str = ""
    version: str = ""
    enabled: bool = True


def load_skills(root):
    root = Path(root).resolve()
    skills_root = root / SKILLS_DIR
    if not skills_root.exists() or not skills_root.is_dir():
        return []

    skills = []
    for path in sorted(skills_root.rglob(SKILL_FILENAME)):
        try:
            resolved = path.resolve()
            if os.path.commonpath([str(root), str(resolved)]) != str(root):
                continue
            if resolved.stat().st_size > MAX_SKILL_FILE_BYTES:
                continue
            raw = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parsed = parse_skill(raw)
        if not parsed["enabled"]:
            continue
        relative_path = resolved.relative_to(root).as_posix()
        name = parsed["name"] or resolved.parent.name
        skills.append(
            SkillInfo(
                name=name,
                description=parsed["description"],
                content=parsed["content"],
                path=relative_path,
                hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                tools=tuple(parsed["tools"]),
                allowed_tools_strict=parsed["allowed_tools_strict"],
                trigger_keywords=tuple(parsed["trigger_keywords"]),
                priority=parsed["priority"],
                conflicts_with=tuple(parsed["conflicts_with"]),
                when_to_use=parsed["when_to_use"],
                version=parsed["version"],
                enabled=parsed["enabled"],
            )
        )
    return skills


def parse_skill(raw):
    text = str(raw or "").strip()
    metadata = {}
    content = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            frontmatter = parts[1]
            content = parts[2].strip()
            metadata = _parse_frontmatter(frontmatter)
    return {
        "name": str(metadata.get("name", "")).strip(),
        "description": str(metadata.get("description", "")).strip(),
        "content": content,
        "tools": tuple(metadata.get("tools", ())),
        "allowed_tools_strict": bool(metadata.get("allowed_tools_strict", False)),
        "trigger_keywords": tuple(metadata.get("trigger_keywords", ())),
        "priority": int(metadata.get("priority", 0)),
        "conflicts_with": tuple(metadata.get("conflicts_with", ())),
        "when_to_use": str(metadata.get("when_to_use", "")).strip(),
        "version": str(metadata.get("version", "")).strip(),
        "enabled": bool(metadata.get("enabled", True)),
    }


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


def select_skills_with_model(model_client, skills, user_message, limit=DEFAULT_SKILL_LIMIT):
    skills = [skill for skill in list(skills or []) if skill.enabled]
    if not skills:
        return []

    candidates = keyword_prefilter(skills, user_message)
    if not candidates:
        candidates = skills
    if _can_skip_model_for_keyword_candidates(candidates) and len(candidates) <= int(limit or DEFAULT_SKILL_LIMIT):
        return resolve_conflicts(candidates, limit=limit)

    prompt = _selection_prompt(candidates, user_message, limit)
    try:
        raw = model_client.complete(prompt, SKILL_SELECTOR_MAX_TOKENS)
        payload = _parse_selector_response(raw)
    except (json.JSONDecodeError, TypeError, ValueError, RuntimeError):
        return resolve_conflicts(candidates, limit=limit)

    by_name = {skill.name: skill for skill in candidates}
    selected = []
    seen = set()
    for name in payload.get("selected_names", []):
        name = str(name).strip()
        if name in by_name and name not in seen:
            selected.append(by_name[name])
            seen.add(name)
        if len(selected) >= int(limit or DEFAULT_SKILL_LIMIT):
            break
    return resolve_conflicts(selected, limit=limit)


def keyword_prefilter(skills, user_message):
    message = str(user_message or "").lower()
    candidates = []
    for skill in skills:
        if not skill.trigger_keywords:
            candidates.append(skill)
            continue
        if any(keyword.lower() in message for keyword in skill.trigger_keywords):
            candidates.append(skill)
    return candidates


def _can_skip_model_for_keyword_candidates(candidates):
    if not candidates:
        return False
    return all(skill.trigger_keywords for skill in candidates)


def resolve_conflicts(selected_skills, limit=DEFAULT_SKILL_LIMIT):
    ordered = sorted(enumerate(list(selected_skills or [])), key=lambda item: (-int(item[1].priority), item[0]))
    selected = []
    selected_names = set()
    for _, skill in ordered:
        conflicts = set(skill.conflicts_with)
        if conflicts & selected_names:
            continue
        selected.append(skill)
        selected_names.add(skill.name)
        if len(selected) >= int(limit or DEFAULT_SKILL_LIMIT):
            break
    return selected


def compute_active_tools(selected_skills, all_tool_names):
    all_tool_names = {str(name) for name in all_tool_names}
    declared = set()
    strict = False
    for skill in selected_skills or []:
        if skill.tools:
            declared.update(skill.tools)
        strict = strict or bool(skill.allowed_tools_strict)
    if not declared:
        return None, False
    return frozenset(declared & all_tool_names), strict


def _selection_prompt(skills, user_message, limit=DEFAULT_SKILL_LIMIT):
    lines = [
        "You are pico's skill selector.",
        "Choose only the skills that are clearly useful for the user's request.",
        "Return strict JSON only, with this schema: {\"selected_names\":[\"skill-name\"]}",
        f"Select at most {int(limit or DEFAULT_SKILL_LIMIT)} skills. Return an empty list when no skill clearly applies.",
        "",
        "Available skills:",
    ]
    for skill in skills:
        description = skill.description
        if skill.when_to_use:
            description = f"{description} Use when: {skill.when_to_use}".strip()
        lines.append(f"- {skill.name}: {description}")
    lines.extend(["", "User request:", str(user_message or "")])
    return "\n".join(lines)


def _parse_selector_response(raw):
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        return {"selected_names": []}
    selected_names = payload.get("selected_names", [])
    if not isinstance(selected_names, list):
        return {"selected_names": []}
    return {"selected_names": selected_names}


def render_skills(selected_skills):
    selected_skills = list(selected_skills or [])
    if not selected_skills:
        return ""

    lines = ["Skills:"]
    for skill in selected_skills:
        lines.append(f"## Skill: {skill.name or 'unnamed'}")
        if skill.description:
            lines.append(f"Description: {skill.description}")
        if skill.when_to_use:
            lines.append(f"When to use: {skill.when_to_use}")
        if skill.version:
            lines.append(f"Version: {skill.version}")
        if skill.path:
            lines.append(f"Source: {skill.path}")
        if skill.content:
            lines.append(skill.content.strip())
    return "\n".join(lines)


def skill_metadata(selected_skills, rendered_text=""):
    selected_skills = list(selected_skills or [])
    return {
        "selected_count": len(selected_skills),
        "selected_names": [skill.name for skill in selected_skills],
        "selected_paths": [skill.path for skill in selected_skills],
        "selected_hashes": [skill.hash for skill in selected_skills],
        "selected_versions": [skill.version for skill in selected_skills],
        "selected_tools": [list(skill.tools) for skill in selected_skills],
        "selected_strict": [bool(skill.allowed_tools_strict) for skill in selected_skills],
        "rendered_chars": len(str(rendered_text or "")),
    }
