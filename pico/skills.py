"""Local skill loading and prompt rendering.

Skills are repository-local instruction files. The first implementation is
deliberately small: read `.pico/skills/**/SKILL.md`, match them with lexical
tokens, and render selected guidance into the prompt.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


SKILLS_DIR = ".pico/skills"
SKILL_FILENAME = "SKILL.md"
DEFAULT_SKILL_LIMIT = 3
MAX_SKILL_FILE_BYTES = 20_000


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


def select_skills(skills, user_message, limit=DEFAULT_SKILL_LIMIT):
    query_tokens = _tokens(user_message)
    if not query_tokens:
        return []

    ranked = []
    for skill in skills:
        name = str(skill.get("name", ""))
        description = str(skill.get("description", ""))
        content = str(skill.get("content", ""))
        name_tokens = _tokens(name)
        description_tokens = _tokens(description)
        content_tokens = _tokens(content)
        score = 0
        score += 8 * len(query_tokens & name_tokens)
        score += 5 * len(query_tokens & description_tokens)
        score += 2 * len(query_tokens & content_tokens)
        if score > 0:
            ranked.append((score, name, skill))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [skill for _, _, skill in ranked[: int(limit or DEFAULT_SKILL_LIMIT)]]


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


def _tokens(text):
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_-]+", str(text or ""))}
