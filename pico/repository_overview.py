"""One-shot, bounded discovery of repository entry points and layout."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .workspace import IGNORED_PATH_NAMES

_MAX_ENTRIES = 8_000
_MAX_DEPTH = 5
_TIMEOUT_SECONDS = 1.0

_BUILD_MARKERS = {
    "pyproject.toml": ("python", "pyproject"),
    "setup.py": ("python", "setuptools"),
    "setup.cfg": ("python", "setuptools"),
    "requirements.txt": ("python", "requirements"),
    "package.json": ("javascript-typescript", "node"),
    "pnpm-workspace.yaml": ("javascript-typescript", "pnpm-workspace"),
    "pom.xml": ("java", "maven"),
    "build.gradle": ("java-kotlin", "gradle"),
    "build.gradle.kts": ("java-kotlin", "gradle-kotlin"),
    "go.mod": ("go", "go-module"),
    "Cargo.toml": ("rust", "cargo"),
}
_AUTOMATION_MARKERS = {
    "Dockerfile": "docker-build",
    "docker-compose.yml": "docker-compose",
    "docker-compose.yaml": "docker-compose",
    "compose.yml": "docker-compose",
    "compose.yaml": "docker-compose",
    "Makefile": "make",
    "Procfile": "process-file",
}
_LANGUAGE_SUFFIXES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
}
_SOURCE_ROOT_NAMES = frozenset({"src", "app", "apps", "lib", "libs", "packages"})
_TEST_ROOT_NAMES = frozenset({"test", "tests", "spec", "specs"})
_CONFIG_ROOT_NAMES = frozenset({"config", "configs", ".github"})


@dataclass(frozen=True)
class RepositoryMarker:
    path: str
    kind: str


@dataclass(frozen=True)
class RepositoryOverview:
    """Ephemeral launch-time facts; not a persisted protocol or architecture claim."""

    languages: tuple[tuple[str, int], ...]
    build_markers: tuple[RepositoryMarker, ...]
    automation_markers: tuple[RepositoryMarker, ...]
    source_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    config_roots: tuple[str, ...]
    scanned_entries: int
    skipped_entries: int
    truncated: bool

    def to_dict(self):
        return asdict(self)

    def to_prompt(self):
        languages = ", ".join(
            f"{language}({count})" for language, count in self.languages[:8]
        ) or "none observed"
        builds = ", ".join(
            f"{item.kind}:{item.path}" for item in self.build_markers[:12]
        ) or "none observed"
        automation = ", ".join(
            f"{item.kind}:{item.path}" for item in self.automation_markers[:12]
        ) or "none observed"
        return "\n".join(
            [
                "Repository overview (bounded launch-time path evidence; not verified architecture):",
                f"- scan: entries={self.scanned_entries}; skipped={self.skipped_entries}; truncated={self.truncated}",
                f"- languages: {languages}",
                f"- build/workspace markers: {builds}",
                f"- automation markers: {automation}",
                f"- source roots: {', '.join(self.source_roots) or 'none observed'}",
                f"- test roots: {', '.join(self.test_roots) or 'none observed'}",
                f"- config/CI roots: {', '.join(self.config_roots) or 'none observed'}",
            ]
        )


@dataclass
class _OverviewAccumulator:
    language_counts: dict[str, int] = field(default_factory=dict)
    build_markers: list[RepositoryMarker] = field(default_factory=list)
    automation_markers: list[RepositoryMarker] = field(default_factory=list)
    source_roots: set[str] = field(default_factory=set)
    test_roots: set[str] = field(default_factory=set)
    config_roots: set[str] = field(default_factory=set)
    scanned: int = 0
    skipped: int = 0
    truncated: bool = False

    def observe(self, entry, relative, depth, pending):
        self.scanned += 1
        if entry.name in IGNORED_PATH_NAMES:
            self.skipped += 1
            return
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            self.skipped += 1
            return
        child = Path(entry.name) if relative == Path(".") else relative / entry.name
        if stat.S_ISLNK(metadata.st_mode):
            self.skipped += 1
        elif stat.S_ISDIR(metadata.st_mode):
            self._observe_directory(entry, child, depth, pending)
        elif stat.S_ISREG(metadata.st_mode):
            self._observe_file(entry.name, child.as_posix())
        else:
            self.skipped += 1

    def _observe_directory(self, entry, child, depth, pending):
        child_text = child.as_posix()
        lowered = entry.name.lower()
        if lowered in _SOURCE_ROOT_NAMES:
            self.source_roots.add(child_text)
        if lowered in _TEST_ROOT_NAMES:
            self.test_roots.add(child_text)
        if lowered in _CONFIG_ROOT_NAMES:
            self.config_roots.add(child_text)
        if depth < _MAX_DEPTH:
            pending.append((Path(entry.path), child, depth + 1))
        else:
            self.skipped += 1

    def _observe_file(self, name, child_text):
        language = _LANGUAGE_SUFFIXES.get(Path(name).suffix.lower())
        if language:
            self.language_counts[language] = self.language_counts.get(language, 0) + 1
        if name in _BUILD_MARKERS:
            build_language, kind = _BUILD_MARKERS[name]
            self.build_markers.append(RepositoryMarker(child_text, kind))
            self.language_counts.setdefault(build_language, 0)
        if name in _AUTOMATION_MARKERS:
            self.automation_markers.append(
                RepositoryMarker(child_text, _AUTOMATION_MARKERS[name])
            )

    def result(self):
        return RepositoryOverview(
            languages=tuple(
                sorted(
                    self.language_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
            build_markers=tuple(self.build_markers),
            automation_markers=tuple(self.automation_markers),
            source_roots=tuple(sorted(self.source_roots)),
            test_roots=tuple(sorted(self.test_roots)),
            config_roots=tuple(sorted(self.config_roots)),
            scanned_entries=self.scanned,
            skipped_entries=self.skipped,
            truncated=self.truncated,
        )


def _scan_budget_exhausted(accumulator, started):
    return (
        accumulator.scanned >= _MAX_ENTRIES
        or time.monotonic() - started >= _TIMEOUT_SECONDS
    )


def discover_repository_overview(root):
    """Scan once without reading file bodies or following symbolic links."""

    root = Path(root).resolve()
    started = time.monotonic()
    pending = [(root, Path("."), 0)]
    accumulator = _OverviewAccumulator()

    while pending:
        if _scan_budget_exhausted(accumulator, started):
            accumulator.truncated = True
            break
        absolute, relative, depth = pending.pop(0)
        try:
            with os.scandir(absolute) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.lower())
        except OSError:
            accumulator.skipped += 1
            continue
        for entry in entries:
            if _scan_budget_exhausted(accumulator, started):
                accumulator.truncated = True
                break
            accumulator.observe(entry, relative, depth, pending)

    return accumulator.result()
