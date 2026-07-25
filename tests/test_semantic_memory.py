import json
from pathlib import Path

import httpx

from pico.memory import LayeredMemory
from pico.semantic_memory import SemanticMemoryConfig, SemanticMemoryIndex


def _config():
    return SemanticMemoryConfig(
        qdrant_url="https://qdrant.example",
        qdrant_api_key="qdrant-secret",
        embedding_base_url="https://embeddings.example/v1",
        embedding_api_key="embedding-secret",
        embedding_model="text-embedding-v4",
        embedding_dimension=1024,
        collection="pico_test_memory",
    )


def test_qdrant_semantic_index_syncs_and_queries_canonical_memory_ids(tmp_path):
    requests = []
    memory_id = "3ac1eadd-f723-54e8-98fd-cea2a911d0e7"

    def handler(request):
        requests.append(request)
        if request.url.host == "embeddings.example":
            inputs = json.loads(request.content)["input"]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": index, "embedding": [float(index + 1)] * 1024}
                        for index, _ in enumerate(inputs)
                    ]
                },
            )
        if request.method == "GET":
            return httpx.Response(404, json={"status": {"error": "missing"}})
        if request.url.path.endswith("/points/query"):
            return httpx.Response(
                200,
                json={"result": {"points": [{"payload": {"memory_id": memory_id}}]}},
            )
        return httpx.Response(200, json={"result": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    index = SemanticMemoryIndex(_config(), workspace_id="workspace-a", http_client=client)
    manifest = tmp_path / "semantic-index.json"
    note = {
        "memory_id": memory_id,
        "text": "回答时先给结论，再解释实现细节。",
        "type": "feedback",
        "source_path": "entries/feedback.md",
        "updated_at": "2026-07-24T00:00:00+00:00",
    }

    result = index.sync([note], manifest_path=manifest)

    assert result == {"status": "ok", "upserted": 1, "deleted": 0}
    assert json.loads(manifest.read_text(encoding="utf-8"))["notes"][memory_id]
    points_request = next(
        request for request in requests if request.url.path.endswith("/points")
    )
    point = json.loads(points_request.content)["points"][0]
    assert point["payload"]["workspace_id"] == "workspace-a"
    assert point["payload"]["type"] == "feedback"
    embedding_request = next(request for request in requests if request.url.host == "embeddings.example")
    assert json.loads(embedding_request.content)["dimensions"] == 1024
    assert index.search("直接告诉我选择") == [memory_id]

    deleted = index.sync([], manifest_path=manifest)

    assert deleted == {"status": "ok", "upserted": 0, "deleted": 1}
    assert any(request.url.path.endswith("/points/delete") for request in requests)


def test_layered_memory_fuses_local_keywords_with_semantic_ids_and_reads_markdown(tmp_path):
    class SemanticStub:
        enabled = True
        last_error = ""
        last_sync = {"status": "not_run", "upserted": 0, "deleted": 0}

        def __init__(self):
            self.memory_ids = []

        def sync(self, notes, *, manifest_path):
            del manifest_path
            self.memory_ids = [note["memory_id"] for note in notes]
            self.last_sync = {"status": "ok", "upserted": len(notes), "deleted": 0}
            return dict(self.last_sync)

        def search(self, query, *, limit):
            del query, limit
            return list(self.memory_ids)

    semantic = SemanticStub()
    memory = LayeredMemory({}, workspace_root=tmp_path, semantic_index=semantic)

    promoted, superseded = memory.promote_durable(
        [("feedback", "回答时先给结论，再解释实现细节。")]
    )
    selected = memory.retrieval_candidates("直接告诉我该怎么选，别铺垫。")

    assert promoted == ["feedback: 回答时先给结论，再解释实现细节。"]
    assert superseded == []
    assert selected[0]["text"] == "回答时先给结论，再解释实现细节。"
    assert memory.last_retrieval_metadata["strategy"] == "rrf_lexical_plus_qdrant_semantic"
    assert memory.last_retrieval_metadata["lexical_candidates"] == 0
    assert memory.last_retrieval_metadata["semantic_candidates"] == 1
    assert not any(Path(note.get("source_path", "")).suffix == ".py" for note in selected)


def test_semantic_memory_configuration_requires_all_external_credentials():
    assert SemanticMemoryConfig.from_env({}) is None

    try:
        SemanticMemoryConfig.from_env({"PICO_QDRANT_URL": "https://qdrant.example"})
    except ValueError as exc:
        assert "PICO_EMBEDDINGS_API_KEY" in str(exc)
    else:
        raise AssertionError("partial semantic-memory configuration must fail")

    local = SemanticMemoryConfig.from_env(
        {
            "PICO_QDRANT_URL": "http://localhost:6333",
            "PICO_EMBEDDINGS_BASE_URL": "https://embeddings.example/v1",
            "PICO_EMBEDDINGS_API_KEY": "embedding-secret",
            "PICO_EMBEDDINGS_MODEL": "text-embedding-v4",
            "PICO_EMBEDDINGS_DIMENSION": "1024",
        }
    )

    assert local is not None
    assert local.qdrant_api_key == ""
    assert local.embedding_dimension == 1024

    try:
        SemanticMemoryConfig.from_env({"PICO_EMBEDDINGS_DIMENSION": "zero"})
    except ValueError as exc:
        assert "PICO_EMBEDDINGS_DIMENSION" in str(exc)
    else:
        raise AssertionError("invalid embedding dimension must fail")


def test_local_qdrant_requests_omit_an_empty_api_key(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "embeddings.example":
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0] * 1024}]},
            )
        if request.method == "GET":
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(200, json={"result": True})

    config = SemanticMemoryConfig(
        qdrant_url="http://localhost:6333",
        qdrant_api_key="",
        embedding_base_url="https://embeddings.example/v1",
        embedding_api_key="embedding-secret",
        embedding_model="text-embedding-v4",
        embedding_dimension=1024,
    )
    index = SemanticMemoryIndex(
        config,
        workspace_id="workspace-a",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    index.sync(
        [
            {
                "memory_id": "3ac1eadd-f723-54e8-98fd-cea2a911d0e7",
                "text": "本地容器无需 API Key。",
                "type": "project",
            }
        ],
        manifest_path=tmp_path / "semantic-index.json",
    )

    qdrant_requests = [request for request in requests if request.url.host == "localhost"]
    assert qdrant_requests
    assert all("api-key" not in request.headers for request in qdrant_requests)
