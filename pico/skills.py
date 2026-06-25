"""Local skill loading, model selection, and prompt rendering."""

from __future__ import annotations

import json
import hashlib
import os
import re
from pathlib import Path


SKILLS_DIR = ".pico/skills"
SKILL_FILENAME = "SKILL.md"
DEFAULT_SKILL_LIMIT = 3
MAX_SKILL_FILE_BYTES = 20_000
SKILL_SELECTOR_MAX_TOKENS = 200


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
        relative_path = resolved.relative_to(root).as_posix()
        name = parsed["name"] or resolved.parent.name
        skills.append(
            {
                "name": name,
                "description": parsed["description"],
                "content": parsed["content"],
                "path": relative_path,
                "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            }
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
            metadata = _parse_simple_frontmatter(frontmatter)
    return {
        "name": str(metadata.get("name", "")).strip(),
        "description": str(metadata.get("description", "")).strip(),
        "content": content,
    }


def _parse_simple_frontmatter(text):
    metadata = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            metadata[key] = value
    return metadata


def select_skills_with_model(model_client, skills, user_message, limit=DEFAULT_SKILL_LIMIT):
    skills = list(skills or [])
    if not skills:
        return []
    prompt = _selection_prompt(skills, user_message, limit)
    raw = model_client.complete(prompt, SKILL_SELECTOR_MAX_TOKENS)
    try:
        payload = _parse_selector_response(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []

    by_name = {str(skill.get("name", "")).strip(): skill for skill in skills}
    selected = []
    seen = set()
    for name in payload.get("selected_names", []):
        name = str(name).strip()
        if name in by_name and name not in seen:
            selected.append(by_name[name])
            seen.add(name)
        if len(selected) >= int(limit or DEFAULT_SKILL_LIMIT):
            break
    return selected


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
        name = str(skill.get("name", "")).strip()
        description = str(skill.get("description", "")).strip()
        lines.append(f"- {name}: {description}")
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
        name = str(skill.get("name", "")).strip() or "unnamed"
        description = str(skill.get("description", "")).strip()
        path = str(skill.get("path", "")).strip()
        lines.append(f"## Skill: {name}")
        if description:
            lines.append(f"Description: {description}")
        if path:
            lines.append(f"Source: {path}")
        content = str(skill.get("content", "")).strip()
        if content:
            lines.append(content)
    return "\n".join(lines)


def skill_metadata(selected_skills, rendered_text=""):
    selected_skills = list(selected_skills or [])
    return {
        "selected_count": len(selected_skills),
        "selected_names": [str(skill.get("name", "")).strip() for skill in selected_skills],
        "selected_paths": [str(skill.get("path", "")).strip() for skill in selected_skills],
        "selected_hashes": [str(skill.get("hash", "")).strip() for skill in selected_skills],
        "rendered_chars": len(str(rendered_text or "")),
    }
