"""Immutable, redacted tool-output artifacts."""

import hashlib
import json
from pathlib import Path

from .contracts import TOOL_ARTIFACT_ID
from .persistence import write_once_bytes

ARTIFACT_PAGE_MAX_BYTES = 8 * 1024
ARTIFACT_SCHEMA_VERSION = "artifact-v3"


class ArtifactStore:
    def __init__(self, run_store, redactor):
        self.run_store = run_store
        self.redactor = redactor
        self._verified_source = None

    def write_tool_output(self, run_id, call_id, content):
        safe_content = str(self.redactor(str(content)))
        digest = hashlib.sha256(safe_content.encode("utf-8")).hexdigest()
        call_digest = hashlib.sha256(str(call_id).encode("utf-8")).hexdigest()
        artifact_id = f"tool_{call_digest[:16]}_{digest[:10]}"
        root = self.run_store.artifact_dir(run_id).resolve()
        root.mkdir(parents=True, exist_ok=True)
        content_path = self._artifact_path(root, artifact_id, ".txt")
        descriptor_path = self._artifact_path(root, artifact_id, ".json")
        descriptor = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "sha256": digest,
            "size_bytes": len(safe_content.encode("utf-8")),
        }
        self._write_once(content_path, safe_content)
        self._write_once(
            descriptor_path,
            json.dumps(descriptor, indent=2, sort_keys=True) + "\n",
        )
        return descriptor

    def _read_verified(self, run_id, artifact_id):
        root = self.run_store.artifact_dir(run_id).resolve()
        descriptor_path = self._artifact_path(root, artifact_id, ".json")
        if not descriptor_path.exists():
            raise ValueError("artifact descriptor is missing")
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if descriptor.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported artifact schema")
        if descriptor.get("artifact_id") != str(artifact_id):
            raise ValueError("artifact id mismatch")
        content_path = self._artifact_path(root, artifact_id, ".txt")
        if not content_path.exists():
            raise ValueError("artifact content is missing")
        stat = content_path.stat()
        version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        key = (str(content_path), version, descriptor.get("sha256"), descriptor.get("size_bytes"))
        if self._verified_source == key:
            return descriptor, content_path, version
        # Verify immutable content without retaining the complete artifact in
        # memory. A changed file or descriptor invalidates this small metadata
        # cache and takes the full streaming validation path.
        self._verified_source = None
        digest = hashlib.sha256()
        size = 0
        with content_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        digest = digest.hexdigest()
        if digest != descriptor.get("sha256"):
            raise ValueError("artifact digest mismatch")
        if size != int(descriptor.get("size_bytes", -1)):
            raise ValueError("artifact size mismatch")
        after = content_path.stat()
        if version == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            self._verified_source = key
            return descriptor, content_path, version
        raise ValueError("artifact changed while it was being verified")

    @staticmethod
    def _artifact_path(root, artifact_id, suffix):
        root = Path(root).resolve()
        artifact_id = str(artifact_id)
        if not TOOL_ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("invalid artifact id")
        path = (root / f"{artifact_id}{suffix}").resolve()
        if path.parent != root:
            raise ValueError("artifact path escapes its run directory")
        return path

    def read_slice(self, run_id, artifact_id, offset, max_bytes):
        descriptor, content_path, version = self._read_verified(run_id, artifact_id)
        offset = int(offset)
        max_bytes = int(max_bytes)
        if max_bytes < 4:
            raise ValueError("artifact page size must be at least 4 bytes for UTF-8")
        max_bytes = min(max_bytes, ARTIFACT_PAGE_MAX_BYTES)
        total_bytes = int(descriptor["size_bytes"])
        if offset < 0 or offset > total_bytes:
            raise ValueError(
                f"artifact offset {offset} is outside output ({total_bytes} bytes)"
            )
        with content_path.open("rb") as source:
            source.seek(offset)
            page = source.read(max_bytes + 4)
        while offset < total_bytes and page and (page[0] & 0xC0) == 0x80:
            offset += 1
            page = page[1:]
        end_index = min(len(page), max_bytes)
        while (
            end_index > 0
            and offset + end_index < total_bytes
            and end_index < len(page)
            and (page[end_index] & 0xC0) == 0x80
        ):
            end_index -= 1
        end = offset + end_index
        content = page[:end_index].decode("utf-8")
        after = content_path.stat()
        if version != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            self._verified_source = None
            raise ValueError("artifact changed while its page was being read")
        return {
            "descriptor": descriptor,
            "content": content,
            "offset": offset,
            "end_offset": end,
            "total_bytes": total_bytes,
        }

    @staticmethod
    def _write_once(path: Path, content: str):
        encoded = str(content).encode("utf-8")
        if not write_once_bytes(path, encoded):
            existing = path.read_text(encoding="utf-8")
            if existing != content:
                raise RuntimeError(f"immutable artifact collision: {path.name}")
