"""Run-scoped, conflict-safe workspace undo journals."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import IGNORED_PATH_NAMES


UNDO_SCHEMA_VERSION = "run-undo-v1"
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RunUndoError(RuntimeError):
    """Base error for journal creation and restoration failures."""


class RunUndoConflictError(RunUndoError):
    """Raised before restoration when a post-run path changed again."""

    def __init__(self, paths):
        self.paths = tuple(sorted({str(path) for path in paths}))
        super().__init__(
            "workspace changed after the run: " + ", ".join(self.paths)
        )


@dataclass(frozen=True)
class RunUndoResult:
    run_id: str
    dry_run: bool
    already_restored: bool
    restored_paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "already_restored": self.already_restored,
            "restored_paths": list(self.restored_paths),
            "deleted_paths": list(self.deleted_paths),
        }


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _without_blob(state):
    return {
        key: value
        for key, value in dict(state or {"kind": "absent"}).items()
        if key != "blob"
    }


def _states_equal(left, right):
    return _without_blob(left) == _without_blob(right)


def _change_summary(path, before, after):
    before_kind = str((before or {}).get("kind", "absent"))
    after_kind = str((after or {}).get("kind", "absent"))
    if before_kind == "absent":
        return f"created:{path}"
    if after_kind == "absent":
        return f"deleted:{path}"
    return f"modified:{path}"


class RunUndoJournal:
    """Capture first-touch preimages and the expected post-run state."""

    def __init__(self, workspace_root, run_dir, run_id):
        self.workspace_root = Path(workspace_root).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.run_id = str(run_id)
        self.undo_dir = self.run_dir / "undo"
        self.manifest_path = self.undo_dir / "manifest.json"
        self.blob_dir = self.undo_dir / "blobs"
        self.pending_dir = self.undo_dir / "pending"

    def start(self):
        if self.manifest_path.exists():
            return self.summary()
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            self.manifest_path,
            {
                "schema_version": UNDO_SCHEMA_VERSION,
                "run_id": self.run_id,
                "workspace_root": str(self.workspace_root),
                "status": "no_changes",
                "created_at": _now(),
                "updated_at": _now(),
                "entries": {},
            },
        )
        return self.summary()

    def prepare(self, agent, name, args, tool):
        if not tool or not bool(tool.get("risky")):
            return None

        raw_paths = []
        targets = []
        if name in {"write_file", "patch_file"}:
            raw_paths = [str((args or {}).get("path", ""))]

        if raw_paths:
            for raw_path in raw_paths:
                if not raw_path.strip():
                    continue
                targets.append(Path(agent.path(raw_path)))
            mode = "paths"
            scope_paths = self._path_scope(targets)
        else:
            mode = "full"
            scope_paths = self._workspace_scope()

        token = uuid.uuid4().hex
        snapshot_dir = self.pending_dir / token
        pending_blobs = snapshot_dir / "blobs"
        states = {
            relative: self._capture_state(
                self._path(relative),
                blob_dir=pending_blobs,
            )
            for relative in scope_paths
        }
        unsupported = [
            relative
            for relative, state in states.items()
            if state.get("kind") == "unsupported"
        ]
        if unsupported:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise RunUndoError(
                "undo cannot snapshot special workspace paths: "
                + ", ".join(unsupported)
            )
        _write_json_atomic(
            snapshot_dir / "snapshot.json",
            {
                "token": token,
                "mode": mode,
                "targets": [
                    self._relative(path)
                    for path in targets
                ]
                if raw_paths
                else [],
                "states": states,
            },
        )
        return token

    def record(self, token):
        if not token:
            return [], []
        snapshot_dir = self.pending_dir / str(token)
        snapshot_path = snapshot_dir / "snapshot.json"
        if not snapshot_path.exists():
            raise RunUndoError(f"missing pending undo snapshot: {token}")
        pending = json.loads(snapshot_path.read_text(encoding="utf-8"))
        before_states = dict(pending.get("states", {}))
        mode = str(pending.get("mode", ""))
        if mode == "paths":
            targets = [self._path(path) for path in pending.get("targets", [])]
            after_scope = self._path_scope(targets)
        elif mode == "full":
            after_scope = self._workspace_scope()
        else:
            raise RunUndoError(f"invalid pending undo snapshot mode: {mode}")
        after_states = {
            relative: self._capture_state(self._path(relative))
            for relative in after_scope
        }

        changed_paths = [
            relative
            for relative in sorted(set(before_states) | set(after_states))
            if not _states_equal(
                before_states.get(relative, {"kind": "absent"}),
                after_states.get(relative, {"kind": "absent"}),
            )
        ]
        summaries = [
            _change_summary(
                relative,
                before_states.get(relative, {"kind": "absent"}),
                after_states.get(relative, {"kind": "absent"}),
            )
            for relative in changed_paths
        ]

        manifest = self._load_manifest()
        entries = dict(manifest.get("entries", {}))
        for relative in changed_paths:
            after_state = after_states.get(relative, {"kind": "absent"})
            if relative in entries:
                entries[relative]["expected_post"] = _without_blob(after_state)
                entries[relative]["last_recorded_at"] = _now()
                continue

            original = dict(
                before_states.get(relative, {"kind": "absent"})
            )
            if original.get("kind") == "file":
                digest = str(original.get("sha256", ""))
                source = snapshot_dir / str(original.get("blob", ""))
                destination = self.blob_dir / digest
                if not source.exists():
                    raise RunUndoError(
                        f"missing undo preimage for {relative}"
                    )
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                original["blob"] = f"blobs/{digest}"
            entries[relative] = {
                "original": original,
                "expected_post": _without_blob(after_state),
                "first_recorded_at": _now(),
                "last_recorded_at": _now(),
            }

        manifest["entries"] = entries
        manifest["status"] = (
            "available"
            if self._active_paths(entries)
            else "no_changes"
        )
        manifest["updated_at"] = _now()
        _write_json_atomic(self.manifest_path, manifest)
        shutil.rmtree(snapshot_dir)
        return changed_paths, summaries

    def mark_failed(self, token, error):
        manifest = self._load_manifest()
        manifest["status"] = "incomplete"
        manifest["error"] = str(error)
        manifest["updated_at"] = _now()
        if token:
            manifest["pending_token"] = str(token)
        _write_json_atomic(self.manifest_path, manifest)

    def summary(self):
        manifest = self._load_manifest()
        entries = dict(manifest.get("entries", {}))
        active_paths = self._active_paths(entries)
        restored_paths = list(manifest.get("restored_paths", []))
        return {
            "schema_version": UNDO_SCHEMA_VERSION,
            "status": str(manifest.get("status", "no_changes")),
            "available": bool(active_paths)
            and manifest.get("status") == "available",
            "changed_path_count": len(active_paths),
            "changed_paths": active_paths,
            "restored_paths": restored_paths,
            "manifest_path": "undo/manifest.json",
        }

    def restore(self, *, dry_run=False):
        manifest = self._load_manifest()
        if manifest.get("status") == "restored":
            return RunUndoResult(
                run_id=self.run_id,
                dry_run=bool(dry_run),
                already_restored=True,
                restored_paths=tuple(manifest.get("restored_paths", [])),
                deleted_paths=tuple(manifest.get("deleted_paths", [])),
            )
        has_pending = self.pending_dir.exists() and any(
            self.pending_dir.iterdir()
        )
        if manifest.get("status") == "incomplete" or has_pending:
            raise RunUndoError(
                "undo journal is incomplete; refusing an unsafe restoration"
            )

        entries = dict(manifest.get("entries", {}))
        active_paths = self._active_paths(entries)
        conflicts = []
        already_original = set()
        for relative in active_paths:
            entry = entries[relative]
            original = dict(entry.get("original", {}))
            expected = dict(entry.get("expected_post", {}))
            current = self._capture_state(self._path(relative))
            if _states_equal(current, original):
                already_original.add(relative)
                continue
            if not _states_equal(current, expected):
                conflicts.append(relative)

        conflicts.extend(
            self._created_directory_conflicts(
                entries,
                active_paths,
            )
        )
        if conflicts:
            raise RunUndoConflictError(conflicts)

        restore_paths = [
            path for path in active_paths if path not in already_original
        ]
        deleted_paths = [
            path
            for path in restore_paths
            if entries[path].get("original", {}).get("kind") == "absent"
        ]
        self._validate_originals(entries, restore_paths)
        if dry_run:
            return RunUndoResult(
                run_id=self.run_id,
                dry_run=True,
                already_restored=False,
                restored_paths=tuple(restore_paths),
                deleted_paths=tuple(deleted_paths),
            )

        self._apply(entries, restore_paths)
        manifest["status"] = "restored"
        manifest["restored_at"] = _now()
        manifest["restored_paths"] = restore_paths
        manifest["deleted_paths"] = deleted_paths
        manifest["updated_at"] = _now()
        _write_json_atomic(self.manifest_path, manifest)
        self._update_report()
        return RunUndoResult(
            run_id=self.run_id,
            dry_run=False,
            already_restored=False,
            restored_paths=tuple(restore_paths),
            deleted_paths=tuple(deleted_paths),
        )

    def _apply(self, entries, restore_paths):
        restore_set = set(restore_paths)
        absent_paths = [
            path
            for path in restore_paths
            if entries[path].get("original", {}).get("kind") == "absent"
        ]
        for relative in sorted(
            absent_paths,
            key=lambda item: (len(Path(item).parts), item),
            reverse=True,
        ):
            self._remove_path(self._path(relative))

        existing_paths = [
            path for path in restore_paths if path not in set(absent_paths)
        ]
        for relative in sorted(
            existing_paths,
            key=lambda item: (len(Path(item).parts), item),
            reverse=True,
        ):
            original_kind = entries[relative]["original"].get("kind")
            current_kind = self._capture_state(
                self._path(relative)
            ).get("kind")
            if current_kind != "absent" and current_kind != original_kind:
                self._remove_path(self._path(relative))

        directory_paths = [
            path
            for path in existing_paths
            if entries[path]["original"].get("kind") == "directory"
        ]
        for relative in sorted(
            directory_paths,
            key=lambda item: (len(Path(item).parts), item),
        ):
            path = self._path(relative)
            path.mkdir(parents=True, exist_ok=True)

        for relative in sorted(
            existing_paths,
            key=lambda item: (len(Path(item).parts), item),
        ):
            state = dict(entries[relative]["original"])
            kind = state.get("kind")
            path = self._path(relative)
            if kind == "file":
                self._restore_file(path, state)
            elif kind == "symlink":
                self._restore_symlink(path, state)
            elif kind == "directory":
                continue
            else:
                raise RunUndoError(
                    f"unsupported original path kind for {relative}: {kind}"
                )

        for relative in directory_paths:
            state = entries[relative]["original"]
            os.chmod(self._path(relative), int(state["mode"]))

        for relative in restore_set:
            expected = entries[relative]["original"]
            current = self._capture_state(self._path(relative))
            if not _states_equal(current, expected):
                raise RunUndoError(
                    f"restoration verification failed for {relative}"
                )

    def _restore_file(self, path, state):
        content = self._read_blob(self._relative(path), state)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.pico-undo.",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.chmod(temp_path, int(state["mode"]))
        os.replace(temp_path, path)

    def _validate_originals(self, entries, restore_paths):
        supported = {"absent", "directory", "file", "symlink"}
        for relative in restore_paths:
            state = dict(entries[relative].get("original", {}))
            kind = state.get("kind")
            if kind not in supported:
                raise RunUndoError(
                    f"unsupported original path kind for {relative}: {kind}"
                )
            if kind == "file":
                self._read_blob(relative, state)

    def _read_blob(self, relative, state):
        raw_blob = Path(str(state.get("blob", "")))
        if raw_blob.is_absolute() or ".." in raw_blob.parts:
            raise RunUndoError(f"unsafe undo blob for {relative}")
        blob_path = (self.undo_dir / raw_blob).resolve()
        try:
            blob_path.relative_to(self.blob_dir.resolve())
        except ValueError as exc:
            raise RunUndoError(f"unsafe undo blob for {relative}") from exc
        if not blob_path.is_file():
            raise RunUndoError(f"missing undo blob for {relative}")
        content = blob_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != state.get("sha256"):
            raise RunUndoError(f"corrupt undo blob for {relative}")
        return content

    def _restore_symlink(self, path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.parent / (
            f".{path.name}.pico-undo.{uuid.uuid4().hex}"
        )
        os.symlink(str(state["target"]), temp_path)
        os.replace(temp_path, path)

    def _remove_path(self, path):
        try:
            path.lstat()
        except FileNotFoundError:
            return
        if path.is_symlink() or not path.is_dir():
            path.unlink()
            return
        try:
            path.rmdir()
        except OSError as exc:
            raise RunUndoError(
                f"refusing to remove non-empty directory: {self._relative(path)}"
            ) from exc

    def _created_directory_conflicts(self, entries, active_paths):
        active_set = set(active_paths)
        conflicts = []
        for relative in active_paths:
            entry = entries[relative]
            if entry.get("original", {}).get("kind") != "absent":
                continue
            if entry.get("expected_post", {}).get("kind") != "directory":
                continue
            directory = self._path(relative)
            if not directory.is_dir() or directory.is_symlink():
                continue
            for descendant in directory.rglob("*"):
                descendant_relative = self._relative(descendant)
                if descendant_relative not in active_set:
                    conflicts.append(relative)
                    break
        return conflicts

    def _update_report(self):
        report_path = self.run_dir / "report.json"
        if not report_path.exists():
            return
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["undo"] = self.summary()
        _write_json_atomic(report_path, report)

    def _load_manifest(self):
        if not self.manifest_path.exists():
            raise RunUndoError(
                f"run has no undo journal: {self.run_id}"
            )
        manifest = json.loads(
            self.manifest_path.read_text(encoding="utf-8")
        )
        if manifest.get("schema_version") != UNDO_SCHEMA_VERSION:
            raise RunUndoError("unsupported undo journal schema")
        if manifest.get("run_id") != self.run_id:
            raise RunUndoError("undo journal run id mismatch")
        if (
            Path(str(manifest.get("workspace_root", ""))).resolve()
            != self.workspace_root
        ):
            raise RunUndoError("undo journal workspace mismatch")
        return manifest

    def _active_paths(self, entries):
        return [
            relative
            for relative in sorted(entries)
            if not _states_equal(
                entries[relative].get("original", {}),
                entries[relative].get("expected_post", {}),
            )
        ]

    def _workspace_scope(self):
        scope = []
        for path in self.workspace_root.rglob("*"):
            relative = self._relative(path)
            if self._ignored(relative):
                continue
            try:
                path.lstat()
            except OSError:
                continue
            scope.append(relative)
        return sorted(scope)

    def _path_scope(self, targets):
        scope = set()
        for target in targets:
            current = Path(target)
            while current != self.workspace_root:
                scope.add(self._relative(current))
                current = current.parent
        return sorted(scope)

    def _capture_state(self, path, blob_dir=None):
        try:
            stat_result = path.lstat()
        except FileNotFoundError:
            return {"kind": "absent"}
        except OSError as exc:
            raise RunUndoError(
                f"cannot inspect {self._relative(path)}: {exc}"
            ) from exc

        mode = stat.S_IMODE(stat_result.st_mode)
        if stat.S_ISLNK(stat_result.st_mode):
            return {
                "kind": "symlink",
                "target": os.readlink(path),
            }
        if stat.S_ISDIR(stat_result.st_mode):
            return {"kind": "directory", "mode": mode}
        if not stat.S_ISREG(stat_result.st_mode):
            return {"kind": "unsupported", "mode": mode}

        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        state = {
            "kind": "file",
            "mode": mode,
            "size": len(content),
            "sha256": digest,
        }
        if blob_dir is not None:
            blob_dir.mkdir(parents=True, exist_ok=True)
            blob_path = blob_dir / digest
            if not blob_path.exists():
                blob_path.write_bytes(content)
            state["blob"] = f"blobs/{digest}"
        return state

    def _path(self, relative):
        raw = Path(str(relative))
        if raw.is_absolute() or ".." in raw.parts:
            raise RunUndoError(f"unsafe undo path: {relative}")
        path = self.workspace_root.joinpath(*raw.parts)
        try:
            path.relative_to(self.workspace_root)
        except ValueError as exc:
            raise RunUndoError(f"unsafe undo path: {relative}") from exc
        return path

    def _relative(self, path):
        try:
            return Path(path).relative_to(self.workspace_root).as_posix()
        except ValueError as exc:
            raise RunUndoError(f"path escapes workspace: {path}") from exc

    def _ignored(self, relative):
        path = Path(relative)
        if path.name.startswith(".env"):
            return True
        return any(
            part in IGNORED_PATH_NAMES
            for part in path.parts
        )


def restore_run(workspace_root, run_id, *, dry_run=False):
    workspace_root = Path(workspace_root).resolve()
    run_id = str(run_id or "").strip()
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise RunUndoError(f"invalid run id: {run_id}")
    runs_root = workspace_root / ".pico" / "runs"
    run_dir = (runs_root / run_id).resolve()
    try:
        run_dir.relative_to(runs_root.resolve())
    except ValueError as exc:
        raise RunUndoError(f"invalid run id: {run_id}") from exc
    if not run_dir.is_dir():
        raise RunUndoError(f"run not found: {run_id}")
    journal = RunUndoJournal(workspace_root, run_dir, run_id)
    return journal.restore(dry_run=dry_run)
