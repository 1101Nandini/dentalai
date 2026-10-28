"""
app.services.message_service — backward-compatibility shim.

All existing imports keep working unchanged:
    from app.services.message_service import MessageService
"""
from app.services.message_service.message_service import MessageService  # noqa: F401

__all__ = ["MessageService"]
