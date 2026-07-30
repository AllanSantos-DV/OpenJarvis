"""native-java memory backend -- project-scoped semantic memory over HTTP.

`native-java <https://github.com/AllanSantos-DV/native-java>`_ is a standalone
memory server that keeps an embedding model resident and serves semantic search
to every tool on the machine over a local REST API.

Two things make it a better fit here than the built-in backends:

* **No Rust build.** The bundled ``sqlite`` backend requires the ``openjarvis_rust``
  extension (maturin + rustc >= 1.88) and raises
  :class:`~openjarvis.tools.storage._stubs.MemoryBackendUnavailable` without it.
  This backend only needs HTTP.
* **Project scoping.** Every document is stamped with a ``project_id`` in its
  metadata and every query filters on it, so memory about one project never
  bleeds into another -- something a flat store cannot express.

The daemon picks a free port at startup and announces it in
``~/.mcp-memory/run/daemon.json``, so the port is discovered at call time rather
than configured.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx

from openjarvis.core.registry import MemoryRegistry
from openjarvis.tools.storage._stubs import MemoryBackend, RetrievalResult

logger = logging.getLogger(__name__)

#: Where the daemon announces the port it bound to.
_RUN_DIR_ENV = "MCP_RUN_DIR"
_DEFAULT_RUN_DIR = Path.home() / ".mcp-memory" / "run"

_DEFAULT_TIMEOUT = 15.0


def discover_base_url() -> Optional[str]:
    """Return the running daemon's base URL, or ``None`` if it is not announced.

    The URL is read from ``daemon.json`` instead of being configured, because the
    daemon binds an ephemeral port and re-announces a new one whenever it
    restarts or self-updates.
    """
    run_dir = Path(os.environ.get(_RUN_DIR_ENV) or _DEFAULT_RUN_DIR)
    announce = run_dir / "daemon.json"
    try:
        payload = json.loads(announce.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    url = payload.get("url")
    if url:
        return str(url).rstrip("/")

    port = payload.get("port")
    return f"http://127.0.0.1:{port}" if port else None


@MemoryRegistry.register("native-java")
class NativeJavaMemoryBackend(MemoryBackend):
    """Semantic memory backed by the native-java daemon, scoped to one project.

    ``project_id`` is merged into the metadata of every stored document and into
    the filter of every query. Callers may still attach their own metadata; only
    the ``project_id`` key is reserved.
    """

    backend_id = "native-java"

    def __init__(
        self,
        *,
        project_id: str = "",
        base_url: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._project_id = project_id
        self._explicit_base_url = base_url.rstrip("/") if base_url else ""
        self._timeout = timeout

    @property
    def base_url(self) -> Optional[str]:
        """Resolved daemon URL: explicit override, else freshly discovered."""
        return self._explicit_base_url or discover_base_url()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
    ) -> Any:
        base = self.base_url
        if not base:
            raise RuntimeError(
                "native-java memory daemon is not running (no daemon.json in "
                f"{os.environ.get(_RUN_DIR_ENV) or _DEFAULT_RUN_DIR}). Start the "
                "server, or set MCP_RUN_DIR if it announces elsewhere."
            )

        response = httpx.request(
            method,
            f"{base}{path}",
            json=json_body,
            timeout=self._timeout,
        )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _scoped_metadata(
        self, extra: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Merge caller metadata with the project scope."""
        metadata: Dict[str, Any] = dict(extra or {})
        if self._project_id:
            metadata["project_id"] = self._project_id
        return metadata

    def store(
        self,
        content: str,
        *,
        source: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Persist *content* stamped with the project scope; return its id."""
        payload = dict(metadata or {})
        if source:
            payload.setdefault("source", source)

        body = {"content": content, "metadata": self._scoped_metadata(payload)}
        result = self._request("POST", "/api/v1/documents", json_body=body) or {}
        return str(result.get("id", ""))

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        **kwargs: Any,
    ) -> List[RetrievalResult]:
        """Semantic search restricted to this project's documents."""
        body: Dict[str, Any] = {
            "query": query,
            "topK": top_k,
            "metadata": self._scoped_metadata(kwargs.get("metadata")),
        }
        if kwargs.get("min_score") is not None:
            body["minScore"] = kwargs["min_score"]

        result = self._request("POST", "/api/v1/search", json_body=body) or {}

        results: List[RetrievalResult] = []
        for hit in result.get("results", []):
            results.append(
                RetrievalResult(
                    content=str(hit.get("text", "")),
                    score=float(hit.get("score", 0.0)),
                    source=str(hit.get("documentId", "")),
                    metadata={
                        "document_id": hit.get("documentId", ""),
                        "chunk_index": hit.get("chunkIndex"),
                        "project_id": self._project_id,
                    },
                )
            )
        return results

    def delete(self, doc_id: str) -> bool:
        """Delete one document. Returns False when it did not exist."""
        try:
            self._request("DELETE", f"/api/v1/documents/{quote(doc_id, safe='')}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return False
            raise
        return True

    def clear(self) -> None:
        """Delete every document of *this project only*.

        The daemon is shared across projects, so a scope is mandatory here:
        clearing without one would wipe other projects' memory. When no
        ``project_id`` is configured the call refuses instead of guessing.
        """
        if not self._project_id:
            raise RuntimeError(
                "Refusing to clear a shared memory daemon without a project_id: "
                "that would delete other projects' documents."
            )

        for doc_id in self._list_document_ids():
            self.delete(doc_id)

    def _list_document_ids(self) -> List[str]:
        """Ids of this project's documents (empty when the scope is unset)."""
        params = {"metadata.project_id": self._project_id, "limit": 1000}
        path = f"/api/v1/documents?{urlencode(params)}"
        result = self._request("GET", path) or {}
        return [str(doc.get("id")) for doc in result.get("data", []) if doc.get("id")]

    def health(self) -> bool:
        """True when the daemon answers ``/health`` and its checks are green."""
        try:
            result = self._request("GET", "/health") or {}
        except Exception as exc:  # noqa: BLE001 -- discovery must not hard-fail
            logger.debug("native-java memory health check failed: %s", exc)
            return False
        return str(result.get("status", "")).lower() in {"healthy", "ok", "up"}


__all__ = ["NativeJavaMemoryBackend", "discover_base_url"]
