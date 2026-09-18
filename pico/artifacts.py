"""Write-once, redacted and pageable tool-output artifacts."""

import hashlib
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path

from .contracts import TOOL_ARTIFACT_ID
from .persistence import write_once_bytes

ARTIFACT_PAGE_MAX_BYTES = 8 * 1024
COMMAND_LOG_MAX_BYTES = 16 * 1024 * 1024


def head_tail(text, max_chars=2000):
    """Bound a preview without losing the final error or summary."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n[... middle omitted; consult referenced output ...]\n" + text[-half:]


class CommandOutputLog:
    """Private, bounded spool; publish only after whole-stream redaction.

    Anonymous temporary files keep unredacted bytes out of persistent artifacts.
    Redacting after capture also handles secrets split across pipe reads.
    """

    def __init__(self, store, run_id, call_id, *, max_bytes=COMMAND_LOG_MAX_BYTES):
        self.store, self.run_id, self.call_id = store, run_id, call_id
        self.max_bytes = max_bytes
        self.files = {}
        self.stack = ExitStack()
        self.size = 0
        self.omitted_bytes = 0
        self.error = ""

    def write(self, stream, chunk):
        selected = chunk[:max(0, self.max_bytes - self.size)]
        self.omitted_bytes += len(chunk) - len(selected)
        if self.error or not selected:
            return
        try:
            if stream not in self.files:
                self.files[stream] = self.stack.enter_context(tempfile.TemporaryFile(mode="w+b"))  # noqa: SIM115 - ExitStack owns the file lifetime
            self.files[stream].write(selected)
            self.size += len(selected)
        except OSError as exc:
            self.error = f"command output could not be saved: {exc}"

    def finish(self):
        if self.error:
            return {}
        try:
            sections = []
            for stream, source in self.files.items():
                source.seek(0)
                text = source.read().decode("utf-8", errors="replace")
                # If capped mid-secret, do not publish the final incomplete line.
                if self.omitted_bytes:
                    text = text[:text.rfind("\n") + 1]
                sections.append(f"## {stream}\n{text}")
            content = "\n".join(sections)
            if self.omitted_bytes:
                content += f"\n[Log incomplete: capture limit reached; {self.omitted_bytes} bytes not saved.]"
            if len(content.encode("utf-8")) <= ARTIFACT_PAGE_MAX_BYTES and not self.omitted_bytes:
                return {}
            return self.store.write_tool_output(self.run_id, self.call_id, content)
        except OSError as exc:
            self.error = f"command output could not be saved: {exc}"
            return {}

    def close(self):
        self.stack.close()


class ArtifactStore:
    def __init__(self, run_store, redactor):
        self.run_store = run_store
        self.redactor = redactor

    def write_tool_output(self, run_id, call_id, content):
        safe_content = str(self.redactor(str(content)))
        encoded = safe_content.encode("utf-8")
        call_digest = hashlib.sha256(str(call_id).encode("utf-8")).hexdigest()
        artifact_id = f"tool_{call_digest[:16]}_{uuid.uuid4().hex[:10]}"
        root = self.run_store.artifact_dir(run_id).resolve()
        root.mkdir(parents=True, exist_ok=True)
        content_path = self._artifact_path(root, artifact_id, ".txt")
        descriptor = {
            "artifact_id": artifact_id,
            "size_bytes": len(encoded),
        }
        if not write_once_bytes(content_path, encoded):
            raise RuntimeError(f"artifact id collision: {artifact_id}")
        return descriptor

    def _source(self, run_id, artifact_id):
        root = self.run_store.artifact_dir(run_id).resolve()
        content_path = self._artifact_path(root, artifact_id, ".txt")
        if not content_path.exists():
            raise ValueError("artifact content is missing")
        stat = content_path.stat()
        version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        descriptor = {
            "artifact_id": str(artifact_id),
            "size_bytes": stat.st_size,
        }
        return descriptor, content_path, version

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
        descriptor, content_path, version = self._source(run_id, artifact_id)
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
            raise ValueError("artifact changed while its page was being read")
        return {
            "descriptor": descriptor,
            "content": content,
            "offset": offset,
            "end_offset": end,
            "total_bytes": total_bytes,
        }
