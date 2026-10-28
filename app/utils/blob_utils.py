"""
Blob download utility.
Complements file_utils.py (which has upload/delete/SAS).
"""
import logging
from azure.storage.blob.aio import BlobServiceClient
from azure.core.exceptions import AzureError
from app.config import settings

logger = logging.getLogger(__name__)


async def download_blob(blob_name: str) -> bytes:
    """Download a blob's content as bytes from Azure Storage."""
    try:
        async with BlobServiceClient.from_connection_string(
            settings.AZURE_STORAGE_CONNECTION_STRING
        ) as client:
            container_client = client.get_container_client(settings.AZURE_BLOB_CONTAINER_NAME)
            blob_client = container_client.get_blob_client(blob_name)
            stream = await blob_client.download_blob()
            return await stream.readall()
    except AzureError as e:
        logger.error(f"Failed to download blob '{blob_name}': {e}")
        raise RuntimeError(f"Could not download blob: {str(e)}")
