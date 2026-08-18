"""Strict contracts at the PR-review boundary."""

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REVIEW_SCHEMA_VERSION = "pico-review-v1"
MAX_INLINE_DIFF_CHARS = 12_000


def normalize_repository_path(value):
    path = str(value or "").strip()
    parsed = PurePosixPath(path)
    normalized = parsed.as_posix()
    if (
        normalized in {"", "."}
        or "\\" in path
        or parsed.is_absolute()
        or ".." in parsed.parts
    ):
        raise ValueError("review paths must be relative POSIX repository paths")
    return normalized


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=240)
    base_sha: str = Field(min_length=1, max_length=120)
    head_sha: str = Field(min_length=1, max_length=120)
    changed_files: list[str] = Field(min_length=1, max_length=200)
    diff: str = Field(min_length=1, max_length=MAX_INLINE_DIFF_CHARS)

    @field_validator("changed_files")
    @classmethod
    def validate_changed_files(cls, values):
        normalized = [normalize_repository_path(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("changed_files must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_revisions(self):
        if self.base_sha == self.head_sha:
            raise ValueError("base_sha and head_sha must differ")
        return self


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(default="", pattern=r"^(?:|finding_[a-f0-9]{16})$")
    category: Literal[
        "security", "reliability", "correctness", "performance", "maintainability"
    ]
    severity: Literal["critical", "high", "medium", "low"]
    confidence: float = Field(ge=0.0, le=1.0)
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    cwe: str = Field(default="", pattern=r"^(?:|CWE-[0-9]{1,5})$")
    title: str = Field(min_length=1, max_length=160)
    explanation: str = Field(min_length=1, max_length=2_000)
    evidence: str = Field(min_length=1, max_length=2_000)
    suggested_fix: str = Field(default="", max_length=2_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value):
        return normalize_repository_path(value)

    @model_validator(mode="after")
    def validate_lines(self):
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[REVIEW_SCHEMA_VERSION] = REVIEW_SCHEMA_VERSION
    review_id: str = ""
    run_ids: list[str] = Field(default_factory=list, max_length=100)
    policy_version: str = ""
    policy_digest: str = ""
    verdict: Literal["clean", "findings"]
    summary: str = Field(min_length=1, max_length=1_000)
    findings: list[Finding] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_verdict(self):
        if self.verdict == "clean" and self.findings:
            raise ValueError("clean reports must not contain findings")
        if self.verdict == "findings" and not self.findings:
            raise ValueError("findings reports require at least one finding")
        return self
