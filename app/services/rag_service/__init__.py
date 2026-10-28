"""
app.services.rag_service — backward-compatibility shim.

All existing imports keep working unchanged:
    from app.services.rag_service import RagService
"""
from app.services.rag_service.retrieval import RagService  # noqa: F401

__all__ = ["RagService"]
