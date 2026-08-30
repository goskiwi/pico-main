"""Immutable, redacted tool-output artifacts."""

import hashlib
import json
import re
from pathlib import Path

from .contracts import TOOL_ARTIFACT_ID
from .persistence import write_once_bytes

ARTIFACT_PAGE_MAX_BYTES = 8 * 1024
INTERNAL_ARTIFACT_ID = re.compile(
    r"^(?:preimage|diff)_[a-f0-9]{16}_[a-f0-9]{10}$"
)
ARTIFACT_SCHEMA_VERSION = "artifact-v3"


class ArtifactStore:
    def __init__(self, run_store, redactor):
        self.run_store = run_store
        self.redactor = redactor

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

    def write_workspace_preimage(self, run_id, call_id, logical_path, content):
        raw_content = str(content)
        data = raw_content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        key_digest = hashlib.sha256(
            f"{call_id}:{logical_path}".encode()
        ).hexdigest()
        artifact_id = f"preimage_{key_digest[:16]}_{digest[:10]}"
        return self._write_internal(
            run_id,
            artifact_id,
            raw_content,
            kind="workspace_preimage",
            metadata={"path": str(logical_path)},
        )

    def write_final_diff(self, run_id, content):
        raw_content = str(content)
        digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        run_digest = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
        artifact_id = f"diff_{run_digest[:16]}_{digest[:10]}"
        return self._write_internal(
            run_id,
            artifact_id,
            raw_content,
            kind="final_workspace_diff",
            metadata={},
        )

    def _write_internal(self, run_id, artifact_id, content, *, kind, metadata):
        data = str(content).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        root = self.run_store.artifact_dir(run_id).resolve()
        root.mkdir(parents=True, exist_ok=True)
        content_path = self._internal_artifact_path(root, artifact_id, ".txt")
        descriptor_path = self._internal_artifact_path(root, artifact_id, ".json")
        descriptor = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "kind": str(kind),
            "sha256": digest,
            "size_bytes": len(data),
            **dict(metadata),
        }
        self._write_once(content_path, str(content))
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
        data = content_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != descriptor.get("sha256"):
            raise ValueError("artifact digest mismatch")
        if len(data) != int(descriptor.get("size_bytes", -1)):
            raise ValueError("artifact size mismatch")
        return descriptor, data

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

    @staticmethod
    def _internal_artifact_path(root, artifact_id, suffix):
        root = Path(root).resolve()
        artifact_id = str(artifact_id)
        if not INTERNAL_ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("invalid internal artifact id")
        path = (root / f"{artifact_id}{suffix}").resolve()
        if path.parent != root:
            raise ValueError("internal artifact path escapes its run directory")
        return path

    def read_internal(self, run_id, artifact_id, *, expected_kind=None):
        root = self.run_store.artifact_dir(run_id).resolve()
        descriptor_path = self._internal_artifact_path(root, artifact_id, ".json")
        content_path = self._internal_artifact_path(root, artifact_id, ".txt")
        if not descriptor_path.is_file() or not content_path.is_file():
            raise ValueError("internal artifact is missing")
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        if descriptor.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported internal artifact schema")
        if descriptor.get("artifact_id") != str(artifact_id):
            raise ValueError("internal artifact id mismatch")
        if expected_kind is not None and descriptor.get("kind") != expected_kind:
            raise ValueError("internal artifact kind mismatch")
        data = content_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != descriptor.get("sha256"):
            raise ValueError("internal artifact digest mismatch")
        if len(data) != int(descriptor.get("size_bytes", -1)):
            raise ValueError("internal artifact size mismatch")
        return descriptor, data

    def read_internal_text(self, run_id, artifact_id):
        _descriptor, data = self.read_internal(run_id, artifact_id)
        return data.decode("utf-8")

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
