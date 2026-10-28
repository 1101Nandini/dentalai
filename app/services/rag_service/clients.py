"""
Shared lazy-initialized clients for the RAG pipeline.

Centralizes connection reuse for embeddings, vision API, and Azure Search.
Replaces the unbounded dict cache with a bounded LRU (max 512 entries) to
prevent memory leaks in long-running servers.
"""
import collections
import logging
from typing import Dict, List, Optional

import httpx
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient as AsyncSearchClient
from langchain_openai import AzureOpenAIEmbeddings

from app.config import settings

logger = logging.getLogger(__name__)

# ── Embedding clients ──────────────────────────────────────────────────────────
_embed_client_3072: Optional[AzureOpenAIEmbeddings] = None
_embed_client_1024: Optional[AzureOpenAIEmbeddings] = None

# ── Vision HTTP client ─────────────────────────────────────────────────────────
_vision_http: Optional[httpx.AsyncClient] = None

# ── Azure Search clients ───────────────────────────────────────────────────────
_search_clients: Dict[str, AsyncSearchClient] = {}

# ── Bounded LRU embedding cache (max 512 entries, ~200 MB worst-case) ─────────
_MAX_CACHE_SIZE = 512
_embedding_cache: collections.OrderedDict = collections.OrderedDict()


def cache_get(key: str) -> Optional[List[float]]:
    """Return a cached embedding vector (LRU order updated on hit)."""
    if key in _embedding_cache:
        _embedding_cache.move_to_end(key)
        return _embedding_cache[key]
    return None


def cache_set(key: str, value: List[float]) -> None:
    """Store an embedding vector, evicting the oldest entry when full."""
    _embedding_cache[key] = value
    _embedding_cache.move_to_end(key)
    if len(_embedding_cache) > _MAX_CACHE_SIZE:
        evicted_key, _ = _embedding_cache.popitem(last=False)
        logger.debug("[CACHE] Evicted oldest entry: %s…", evicted_key[:40])


def get_embed_client(dimensions: int) -> AzureOpenAIEmbeddings:
    """Return a cached AzureOpenAIEmbeddings client for the given dimension."""
    global _embed_client_3072, _embed_client_1024
    endpoint = settings.AZURE_OPENAI_EMBEDDING_ENDPOINT
    api_key = settings.AZURE_OPENAI_EMBEDDING_KEY

    if dimensions == 3072:
        if _embed_client_3072 is None:
            _embed_client_3072 = AzureOpenAIEmbeddings(
                azure_endpoint=endpoint,
                azure_deployment=settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
                openai_api_version="2024-12-01-preview",
                api_key=api_key,
                dimensions=3072,
            )
        return _embed_client_3072
    else:
        if _embed_client_1024 is None:
            _embed_client_1024 = AzureOpenAIEmbeddings(
                azure_endpoint=endpoint,
                azure_deployment=settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
                openai_api_version="2023-05-15",
                api_key=api_key,
                dimensions=1024,
            )
        return _embed_client_1024


def get_vision_http() -> httpx.AsyncClient:
    """Return a cached httpx client for Azure Vision API with proper limits."""
    global _vision_http
    if _vision_http is None or _vision_http.is_closed:
        _vision_http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _vision_http


def get_search_client(index_name: str) -> AsyncSearchClient:
    """Return a cached AsyncSearchClient for the given Azure Search index."""
    if index_name not in _search_clients:
        _search_clients[index_name] = AsyncSearchClient(
            endpoint=settings.AZURE_SEARCH_ENDPOINT,
            index_name=index_name,
            credential=AzureKeyCredential(settings.AZURE_SEARCH_KEY),
        )
    return _search_clients[index_name]
