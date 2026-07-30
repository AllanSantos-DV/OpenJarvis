"""Unit tests for NativeJavaMemoryBackend (no daemon required)."""

from __future__ import annotations

import json

import httpx
import pytest

from openjarvis.core.registry import MemoryRegistry
from openjarvis.tools.storage.native_java import (
    NativeJavaMemoryBackend,
    discover_base_url,
)

PROJECT = "owner/project"


@pytest.fixture(autouse=True)
def _register_native_java():
    """Re-register after any registry clear."""
    if not MemoryRegistry.contains("native-java"):
        MemoryRegistry.register_value("native-java", NativeJavaMemoryBackend)


@pytest.fixture
def captured(monkeypatch):
    """Capture outgoing requests and script the responses."""
    calls = []
    responses = {}

    def _request(method, url, **kwargs):
        calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        status, payload = responses.get(
            (method, url.split("?")[0].split("/api")[-1]), (None, None)
        )
        if status is None:
            status, payload = responses.get("default", (200, {}))
        request = httpx.Request(method, url)
        content = b"" if payload is None else json.dumps(payload).encode()
        return httpx.Response(status, content=content, request=request)

    monkeypatch.setattr(httpx, "request", _request)
    return calls, responses


def _backend(**kwargs):
    kwargs.setdefault("project_id", PROJECT)
    kwargs.setdefault("base_url", "http://127.0.0.1:9999")
    return NativeJavaMemoryBackend(**kwargs)


def test_registered():
    assert MemoryRegistry.contains("native-java")
    assert MemoryRegistry.get("native-java") is NativeJavaMemoryBackend


def test_discover_base_url_reads_announce_file(tmp_path, monkeypatch):
    """The daemon binds an ephemeral port, so the URL is read, not configured."""
    (tmp_path / "daemon.json").write_text(
        json.dumps({"url": "http://127.0.0.1:53711", "port": 53711}), encoding="utf-8"
    )
    monkeypatch.setenv("MCP_RUN_DIR", str(tmp_path))

    assert discover_base_url() == "http://127.0.0.1:53711"


def test_discover_base_url_falls_back_to_port(tmp_path, monkeypatch):
    (tmp_path / "daemon.json").write_text(json.dumps({"port": 4321}), encoding="utf-8")
    monkeypatch.setenv("MCP_RUN_DIR", str(tmp_path))

    assert discover_base_url() == "http://127.0.0.1:4321"


def test_discover_base_url_none_when_daemon_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RUN_DIR", str(tmp_path))

    assert discover_base_url() is None


def test_request_without_daemon_raises_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RUN_DIR", str(tmp_path))

    with pytest.raises(RuntimeError, match="daemon is not running"):
        NativeJavaMemoryBackend(project_id=PROJECT).store("x")


def test_store_stamps_project_scope(captured):
    calls, responses = captured
    responses["default"] = (200, {"id": "doc-1"})

    doc_id = _backend().store("conteudo", source="probe", metadata={"kind": "note"})

    assert doc_id == "doc-1"
    body = calls[0]["json"]
    assert body["metadata"]["project_id"] == PROJECT
    assert body["metadata"]["kind"] == "note"
    assert body["metadata"]["source"] == "probe"


def test_retrieve_filters_by_project_and_maps_results(captured):
    calls, responses = captured
    responses["default"] = (
        200,
        {
            "results": [
                {"text": "achado", "score": 0.7, "documentId": "d1", "chunkIndex": 2}
            ]
        },
    )

    results = _backend().retrieve("pergunta", top_k=3)

    assert calls[0]["json"]["metadata"]["project_id"] == PROJECT
    assert calls[0]["json"]["topK"] == 3
    assert len(results) == 1
    assert results[0].content == "achado"
    assert results[0].score == 0.7
    assert results[0].source == "d1"
    assert results[0].metadata["chunk_index"] == 2
    assert results[0].metadata["project_id"] == PROJECT


def test_delete_returns_false_on_404(captured):
    _, responses = captured
    responses["default"] = (404, {"error": "not found"})

    assert _backend().delete("missing") is False


def test_delete_propagates_other_errors(captured):
    _, responses = captured
    responses["default"] = (500, {"error": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        _backend().delete("doc-1")


def test_clear_without_project_id_refuses(captured):
    """The daemon is shared, so an unscoped clear would wipe other projects."""
    with pytest.raises(RuntimeError, match="Refusing to clear"):
        _backend(project_id="").clear()


def test_clear_deletes_only_this_projects_documents(captured):
    calls, responses = captured
    responses["default"] = (200, {"data": [{"id": "a"}, {"id": "b"}]})

    _backend().clear()

    listed = calls[0]
    assert "metadata.project_id" in listed["url"]
    assert PROJECT.replace("/", "%2F") in listed["url"]
    assert [c["method"] for c in calls[1:]] == ["DELETE", "DELETE"]


def test_health_true_only_when_status_is_green(captured):
    _, responses = captured
    responses["default"] = (200, {"status": "healthy"})
    assert _backend().health() is True

    responses["default"] = (200, {"status": "degraded"})
    assert _backend().health() is False


def test_health_false_when_daemon_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RUN_DIR", str(tmp_path))

    assert NativeJavaMemoryBackend(project_id=PROJECT).health() is False
