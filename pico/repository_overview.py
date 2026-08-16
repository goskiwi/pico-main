"""One-shot, bounded discovery of repository entry points and layout."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import asdict, dataclass
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


def discover_repository_overview(root):
    """Scan once without reading file bodies or following symbolic links."""

    root = Path(root).resolve()
    started = time.monotonic()
    queue = [(root, Path("."), 0)]
    language_counts: dict[str, int] = {}
    build_markers = []
    automation_markers = []
    source_roots = set()
    test_roots = set()
    config_roots = set()
    scanned = 0
    skipped = 0
    truncated = False

    while queue:
        if scanned >= _MAX_ENTRIES or time.monotonic() - started >= _TIMEOUT_SECONDS:
            truncated = True
            break
        absolute, relative, depth = queue.pop(0)
        try:
            with os.scandir(absolute) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.lower())
        except OSError:
            skipped += 1
            continue
        for entry in entries:
            if scanned >= _MAX_ENTRIES or time.monotonic() - started >= _TIMEOUT_SECONDS:
                truncated = True
                break
            scanned += 1
            if entry.name in IGNORED_PATH_NAMES:
                skipped += 1
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                skipped += 1
                continue
            child = Path(entry.name) if relative == Path(".") else relative / entry.name
            child_text = child.as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                skipped += 1
                continue
            if stat.S_ISDIR(metadata.st_mode):
                lowered = entry.name.lower()
                if lowered in _SOURCE_ROOT_NAMES:
                    source_roots.add(child_text)
                if lowered in _TEST_ROOT_NAMES:
                    test_roots.add(child_text)
                if lowered in _CONFIG_ROOT_NAMES:
                    config_roots.add(child_text)
                if depth < _MAX_DEPTH:
                    queue.append((Path(entry.path), child, depth + 1))
                else:
                    skipped += 1
                continue
            if not stat.S_ISREG(metadata.st_mode):
                skipped += 1
                continue
            language = _LANGUAGE_SUFFIXES.get(Path(entry.name).suffix.lower())
            if language:
                language_counts[language] = language_counts.get(language, 0) + 1
            if entry.name in _BUILD_MARKERS:
                build_language, kind = _BUILD_MARKERS[entry.name]
                build_markers.append(RepositoryMarker(child_text, kind))
                language_counts.setdefault(build_language, 0)
            if entry.name in _AUTOMATION_MARKERS:
                automation_markers.append(
                    RepositoryMarker(child_text, _AUTOMATION_MARKERS[entry.name])
                )

    return RepositoryOverview(
        languages=tuple(
            sorted(language_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        build_markers=tuple(build_markers),
        automation_markers=tuple(automation_markers),
        source_roots=tuple(sorted(source_roots)),
        test_roots=tuple(sorted(test_roots)),
        config_roots=tuple(sorted(config_roots)),
        scanned_entries=scanned,
        skipped_entries=skipped,
        truncated=truncated,
    )
