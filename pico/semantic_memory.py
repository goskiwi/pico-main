"""External Qdrant-backed semantic index for stable Pico memories.

Markdown remains the canonical record.  This module stores only a searchable
vector mirror for durable ``user``, ``feedback``, ``project``, and
``reference`` notes; it never indexes source code, task artifacts, or tool
output.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx


RRF_K = 60


@dataclass(frozen=True)
class SemanticMemoryConfig:
    qdrant_url: str
    qdrant_api_key: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    embedding_dimension: int | None = None
    collection: str = "pico_memory"
    timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls, env):
        dimension_raw = str(env.get("PICO_EMBEDDINGS_DIMENSION", "")).strip()
        try:
            embedding_dimension = int(dimension_raw) if dimension_raw else None
        except ValueError as exc:
            raise ValueError("PICO_EMBEDDINGS_DIMENSION must be a positive integer") from exc
        if embedding_dimension is not None and embedding_dimension <= 0:
            raise ValueError("PICO_EMBEDDINGS_DIMENSION must be a positive integer")
        values = {
            "qdrant_url": str(env.get("PICO_QDRANT_URL", "")).strip(),
            "qdrant_api_key": str(env.get("PICO_QDRANT_API_KEY", "")).strip(),
            "embedding_base_url": str(env.get("PICO_EMBEDDINGS_BASE_URL", "")).strip(),
            "embedding_api_key": str(env.get("PICO_EMBEDDINGS_API_KEY", "")).strip(),
            "embedding_model": str(env.get("PICO_EMBEDDINGS_MODEL", "")).strip(),
            "embedding_dimension": embedding_dimension,
            "collection": str(env.get("PICO_QDRANT_COLLECTION", "pico_memory")).strip(),
        }
        configured = [key for key, value in values.items() if key != "collection" and value]
        if not configured:
            return None
        # A local Qdrant container normally has no API key.  Keep the key
        # optional while requiring the endpoints and embedding credentials
        # needed for semantic memory to actually work.
        required = (
            "qdrant_url",
            "embedding_base_url",
            "embedding_api_key",
            "embedding_model",
        )
        missing = [key for key in required if not values[key]]
        if missing:
            env_names = {
                "qdrant_url": "PICO_QDRANT_URL",
                "embedding_base_url": "PICO_EMBEDDINGS_BASE_URL",
                "embedding_api_key": "PICO_EMBEDDINGS_API_KEY",
                "embedding_model": "PICO_EMBEDDINGS_MODEL",
            }
            names = ", ".join(env_names[key] for key in missing)
            raise ValueError(f"incomplete semantic-memory configuration: missing {names}")
        return cls(**values)


class SemanticMemoryIndex:
    """Synchronize canonical durable notes to Qdrant and query them by meaning."""

    def __init__(self, config, *, workspace_id, http_client=None):
        self.config = config
        self.workspace_id = str(workspace_id)
        self._owned_http_client = http_client is None
        self._client = http_client or httpx.Client(timeout=config.timeout_seconds)
        self.last_error = ""
        self.last_sync = {"status": "not_run", "upserted": 0, "deleted": 0}

    @property
    def enabled(self):
        return True

    def close(self):
        if self._owned_http_client:
            self._client.close()
            self._owned_http_client = False

    def sync(self, notes, *, manifest_path):
        """Reconcile the Qdrant mirror with the complete canonical note set."""
        notes = [dict(note) for note in notes if str(note.get("memory_id", "")).strip()]
        manifest_path = Path(manifest_path)
        previous = self._load_manifest(manifest_path)
        current = {
            str(note["memory_id"]): self._content_hash(note)
            for note in notes
        }
        changed = [note for note in notes if previous.get(str(note["memory_id"])) != current[str(note["memory_id"])]]
        deleted_ids = sorted(set(previous) - set(current))
        try:
            if changed:
                vectors = self._embed([note["text"] for note in changed])
                if len(vectors) != len(changed):
                    raise RuntimeError("embedding response count does not match note count")
                self._ensure_collection(len(vectors[0]))
                points = [
                    {
                        "id": note["memory_id"],
                        "vector": vector,
                        "payload": {
                            "workspace_id": self.workspace_id,
                            "memory_id": note["memory_id"],
                            "type": note.get("type", ""),
                            "source_path": note.get("source_path", ""),
                            "content_hash": current[note["memory_id"]],
                            "updated_at": note.get("updated_at", ""),
                        },
                    }
                    for note, vector in zip(changed, vectors)
                ]
                self._qdrant_request(
                    "PUT",
                    f"/collections/{self.config.collection}/points",
                    json={"points": points},
                )
            if deleted_ids:
                self._qdrant_request(
                    "POST",
                    f"/collections/{self.config.collection}/points/delete",
                    json={"points": deleted_ids},
                )
            self._write_manifest(manifest_path, current)
            self.last_error = ""
            self.last_sync = {
                "status": "ok",
                "upserted": len(changed),
                "deleted": len(deleted_ids),
            }
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            self.last_error = str(exc)
            self.last_sync = {
                "status": "error",
                "upserted": 0,
                "deleted": 0,
                "error": self.last_error,
            }
        return dict(self.last_sync)

    def search(self, query, *, limit=12):
        """Return canonical memory IDs ranked by semantic similarity."""
        try:
            vector = self._embed([str(query)])[0]
            payload = self._qdrant_request(
                "POST",
                f"/collections/{self.config.collection}/points/query",
                json={
                    "query": vector,
                    "limit": max(1, int(limit)),
                    "with_payload": ["memory_id"],
                    "filter": {
                        "must": [
                            {
                                "key": "workspace_id",
                                "match": {"value": self.workspace_id},
                            }
                        ]
                    },
                },
            )
            points = payload.get("result", {}).get("points", payload.get("result", []))
            if not isinstance(points, list):
                raise RuntimeError("Qdrant query returned invalid points")
            memory_ids = []
            for point in points:
                point_payload = point.get("payload", {}) if isinstance(point, dict) else {}
                memory_id = str(point_payload.get("memory_id", "")).strip()
                if memory_id and memory_id not in memory_ids:
                    memory_ids.append(memory_id)
            self.last_error = ""
            return memory_ids
        except (httpx.HTTPError, RuntimeError, ValueError, IndexError) as exc:
            self.last_error = str(exc)
            return []

    def _embed(self, texts):
        payload = {"model": self.config.embedding_model, "input": list(texts)}
        if self.config.embedding_dimension is not None:
            payload["dimensions"] = self.config.embedding_dimension
        response = self._client.post(
            self.config.embedding_base_url.rstrip("/") + "/embeddings",
            headers={
                "Authorization": f"Bearer {self.config.embedding_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [item.get("embedding") for item in ordered]
        if not vectors or any(not isinstance(vector, list) or not vector for vector in vectors):
            raise RuntimeError("embedding response contains no vectors")
        if self.config.embedding_dimension is not None and any(
            len(vector) != self.config.embedding_dimension for vector in vectors
        ):
            raise RuntimeError(
                "embedding response dimension does not match "
                f"PICO_EMBEDDINGS_DIMENSION={self.config.embedding_dimension}"
            )
        return vectors

    def _ensure_collection(self, vector_size):
        path = f"/collections/{self.config.collection}"
        response = self._client.get(
            self.config.qdrant_url.rstrip("/") + path,
            headers=self._qdrant_headers(),
        )
        if response.status_code == 404:
            self._qdrant_request(
                "PUT",
                path,
                json={"vectors": {"size": int(vector_size), "distance": "Cosine"}},
            )
            return
        response.raise_for_status()

    def _qdrant_request(self, method, path, *, json):
        response = self._client.request(
            method,
            self.config.qdrant_url.rstrip("/") + path,
            headers=self._qdrant_headers(),
            json=json,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _qdrant_headers(self):
        if not self.config.qdrant_api_key:
            return {}
        return {"api-key": self.config.qdrant_api_key}

    @staticmethod
    def _content_hash(note):
        canonical = json.dumps(
            {
                "text": note.get("text", ""),
                "type": note.get("type", ""),
                "updated_at": note.get("updated_at", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _load_manifest(path):
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        notes = loaded.get("notes", {}) if isinstance(loaded, dict) else {}
        return {str(key): str(value) for key, value in notes.items()} if isinstance(notes, dict) else {}

    @staticmethod
    def _write_manifest(path, notes):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump({"schema_version": 1, "notes": notes}, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(path)


class DisabledSemanticMemoryIndex:
    """Explicit no-network implementation used until service credentials exist."""

    enabled = False
    last_error = "semantic memory is not configured"
    last_sync = {"status": "unconfigured", "upserted": 0, "deleted": 0}

    def sync(self, notes, *, manifest_path):
        del notes, manifest_path
        return dict(self.last_sync)

    def search(self, query, *, limit=12):
        del query, limit
        return []

    def close(self):
        return None


def reciprocal_rank_fusion(rankings, *, k=RRF_K):
    """Fuse ranked ID lists while preserving independent retrieval evidence."""
    scores = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (int(k) + rank)
    return [item_id for item_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]
