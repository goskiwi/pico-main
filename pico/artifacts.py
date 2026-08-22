"""Immutable, redacted tool-output artifacts."""

import hashlib
import json
from pathlib import Path

from .persistence import write_once_bytes

ARTIFACT_PAGE_MAX_BYTES = 8 * 1024


class ArtifactStore:
    def __init__(self, run_store, redactor):
        self.run_store = run_store
        self.redactor = redactor

    def write_tool_output(self, run_id, call_id, content):
        safe_content = str(self.redactor(str(content)))
        digest = hashlib.sha256(safe_content.encode("utf-8")).hexdigest()
        artifact_id = f"tool_{call_id}_{digest[:10]}"
        root = self.run_store.artifact_dir(run_id)
        root.mkdir(parents=True, exist_ok=True)
        content_path = root / f"{artifact_id}.txt"
        descriptor_path = root / f"{artifact_id}.json"
        descriptor = {
            "schema_version": "artifact-v2",
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
        descriptor_path = (root / f"{artifact_id}.json").resolve()
        if descriptor_path.parent != root or not descriptor_path.exists():
            raise ValueError("artifact descriptor is missing")
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if descriptor.get("schema_version") != "artifact-v2":
            raise ValueError("unsupported artifact schema")
        if descriptor.get("artifact_id") != str(artifact_id):
            raise ValueError("artifact id mismatch")
        content_path = (root / f"{artifact_id}.txt").resolve()
        if content_path.parent != root or not content_path.exists():
            raise ValueError("artifact content is missing")
        data = content_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != descriptor.get("sha256"):
            raise ValueError("artifact digest mismatch")
        if len(data) != int(descriptor.get("size_bytes", -1)):
            raise ValueError("artifact size mismatch")
        return descriptor, data

    def read_slice(self, run_id, artifact_id, offset, max_bytes):
        descriptor, data = self._read_verified(run_id, artifact_id)
        offset = int(offset)
        max_bytes = int(max_bytes)
        if max_bytes < 1:
            raise ValueError("artifact page size must be positive")
        max_bytes = min(max_bytes, ARTIFACT_PAGE_MAX_BYTES)
        if offset < 0 or offset > len(data):
            raise ValueError(f"artifact offset {offset} is outside output ({len(data)} bytes)")
        while offset < len(data) and (data[offset] & 0xC0) == 0x80:
            offset += 1
        end = min(len(data), offset + max_bytes)
        while end > offset and end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        content = data[offset:end].decode("utf-8")
        return {
            "descriptor": descriptor,
            "content": content,
            "offset": offset,
            "end_offset": end,
            "total_bytes": len(data),
        }

    @staticmethod
    def _write_once(path: Path, content: str):
        encoded = str(content).encode("utf-8")
        if not write_once_bytes(path, encoded):
            existing = path.read_text(encoding="utf-8")
            if existing != content:
                raise RuntimeError(f"immutable artifact collision: {path.name}")
