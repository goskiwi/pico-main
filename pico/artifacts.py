"""Immutable, redacted tool-output artifacts."""

import hashlib
import json
from pathlib import Path

from .workspace import now

ARTIFACT_PAGE_MAX_BYTES = 8 * 1024


class ArtifactStore:
    def __init__(self, run_store, redactor):
        self.run_store = run_store
        self.redactor = redactor

    def write_tool_output(self, run_id, call_id, tool_name, content):
        safe_content = str(self.redactor(str(content)))
        digest = hashlib.sha256(safe_content.encode("utf-8")).hexdigest()
        artifact_id = f"tool_{call_id}_{digest[:10]}"
        root = self.run_store.artifact_dir(run_id)
        root.mkdir(parents=True, exist_ok=True)
        content_path = root / f"{artifact_id}.txt"
        descriptor_path = root / f"{artifact_id}.json"
        descriptor = {
            "schema_version": "artifact-v1",
            "artifact_id": artifact_id,
            "kind": "tool_output",
            "tool_call_id": str(call_id),
            "tool_name": str(tool_name),
            "sha256": digest,
            "size_bytes": len(safe_content.encode("utf-8")),
            "created_at": now(),
            "content_file": content_path.name,
        }
        self._write_once(content_path, safe_content)
        self._write_once(
            descriptor_path,
            json.dumps(descriptor, indent=2, sort_keys=True) + "\n",
        )
        return descriptor

    def verify(self, run_id, artifact_id):
        root = self.run_store.artifact_dir(run_id).resolve()
        descriptor_path = (root / f"{artifact_id}.json").resolve()
        if descriptor_path.parent != root or not descriptor_path.exists():
            raise ValueError("artifact descriptor is missing")
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if descriptor.get("schema_version") != "artifact-v1":
            raise ValueError("unsupported artifact schema")
        if descriptor.get("artifact_id") != str(artifact_id):
            raise ValueError("artifact id mismatch")
        content_path = (root / str(descriptor.get("content_file", ""))).resolve()
        if content_path.parent != root or not content_path.exists():
            raise ValueError("artifact content is missing")
        content = content_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest != descriptor.get("sha256"):
            raise ValueError("artifact digest mismatch")
        if len(content.encode("utf-8")) != int(descriptor.get("size_bytes", -1)):
            raise ValueError("artifact size mismatch")
        return descriptor

    def read_slice(self, run_id, artifact_id, offset, max_bytes):
        descriptor = self.verify(run_id, artifact_id)
        root = self.run_store.artifact_dir(run_id).resolve()
        content_path = (root / descriptor["content_file"]).resolve()
        data = content_path.read_bytes()
        offset = int(offset)
        max_bytes = min(int(max_bytes), ARTIFACT_PAGE_MAX_BYTES)
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
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError:
            existing = path.read_text(encoding="utf-8")
            if existing != content:
                raise RuntimeError(f"immutable artifact collision: {path.name}")
