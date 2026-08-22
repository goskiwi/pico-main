"""Validated input for one CI failure diagnosis."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TriageCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$")
    repository_root: Path
    revision: str = ""
    failing_command: str = Field(min_length=1)
    verification_command: str = ""
    ci_log: str = Field(min_length=1, max_length=100_000)
    issue: str = Field(default="", max_length=20_000)
    constraints: tuple[str, ...] = Field(default=(), max_length=24)

    @field_validator(
        "revision",
        "verification_command",
        "issue",
    )
    @classmethod
    def strip_text(cls, value):
        return str(value).strip()

    @field_validator("failing_command", "ci_log")
    @classmethod
    def strip_required_text(cls, value):
        text = str(value).strip()
        if not text:
            raise ValueError("required Triage text must not be blank")
        return text

    @field_validator("constraints")
    @classmethod
    def normalize_constraints(cls, value):
        normalized = tuple(str(item).strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("triage constraints must be non-empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("triage constraints must be unique")
        return normalized

    @field_validator("repository_root")
    @classmethod
    def validate_repository(cls, value):
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("triage repository_root must be a directory")
        return root

    @property
    def verifier(self):
        return self.verification_command or self.failing_command

    @classmethod
    def from_json(cls, path):
        path = Path(path).expanduser().resolve()
        value = json.loads(path.read_text(encoding="utf-8"))
        repository = Path(value.get("repository_root", ""))
        if not repository.is_absolute():
            value["repository_root"] = path.parent / repository
        return cls.model_validate(value)
