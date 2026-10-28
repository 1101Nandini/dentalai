import io
import logging
from urllib.parse import quote
from azure.storage.blob.aio import BlobServiceClient
# ADD THIS IMPORT:
from azure.storage.blob import ContentSettings 
from azure.core.exceptions import ResourceExistsError, AzureError
from app.config import settings
from datetime import datetime, timedelta, timezone
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.blob import ContentSettings, generate_blob_sas, BlobSasPermissions
from azure.core.exceptions import ResourceExistsError, AzureError

from app.config import settings

logger = logging.getLogger(__name__)

async def upload_blob(blob_name: str, content: bytes, content_type: str) -> str:
    """Uploads a blob to Azure, ensuring the container exists."""
    try:
        async with BlobServiceClient.from_connection_string(
            settings.AZURE_STORAGE_CONNECTION_STRING
        ) as blob_service_client:
            
            container_client = blob_service_client.get_container_client(settings.AZURE_BLOB_CONTAINER_NAME)
            
            # Ensure container exists
            try:
                await container_client.create_container()
            except ResourceExistsError:
                pass

            blob_client = container_client.get_blob_client(blob_name)
            
            # FIX: Use ContentSettings class instead of a raw dictionary
            await blob_client.upload_blob(
                io.BytesIO(content),
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
            
            return blob_client.url
            
    except AzureError as e:
        logger.error(f"Azure Storage Error: {e}")
        raise RuntimeError(f"Could not upload file to Azure: {str(e)}")

async def create_append_blob(blob_name: str, content_type: str = "audio/webm") -> str:
    """Creates an empty Append Blob in Azure."""
    try:
        async with BlobServiceClient.from_connection_string(
            settings.AZURE_STORAGE_CONNECTION_STRING
        ) as blob_service_client:
            container_client = blob_service_client.get_container_client(settings.AZURE_BLOB_CONTAINER_NAME)
            try:
                await container_client.create_container()
            except ResourceExistsError:
                pass

            blob_client = container_client.get_blob_client(blob_name)
            try:
                await blob_client.create_append_blob(
                    content_settings=ContentSettings(content_type=content_type),
                    if_none_match="*"
                )
            except ResourceExistsError:
                logger.info(f"Blob {blob_name} already exists. Resuming append.")
                pass
            
            return blob_client.url
    except AzureError as e:
        logger.error(f"Azure Storage Error: {e}")
        raise RuntimeError(f"Could not create append blob: {str(e)}")

async def append_to_blob(blob_name: str, content: bytes) -> None:
    """Appends bytes to an existing Append Blob in Azure."""
    try:
        async with BlobServiceClient.from_connection_string(
            settings.AZURE_STORAGE_CONNECTION_STRING
        ) as blob_service_client:
            blob_client = blob_service_client.get_blob_client(
                container=settings.AZURE_BLOB_CONTAINER_NAME, 
                blob=blob_name
            )
            await blob_client.append_block(content)
    except AzureError as e:
        logger.error(f"Azure Storage Error: {e}")
        raise RuntimeError(f"Could not append to blob: {str(e)}")

async def read_blob(blob_name: str) -> bytes:
    """Reads the entire content of a blob into memory."""
    try:
        async with BlobServiceClient.from_connection_string(
            settings.AZURE_STORAGE_CONNECTION_STRING
        ) as blob_service_client:
            blob_client = blob_service_client.get_blob_client(
                container=settings.AZURE_BLOB_CONTAINER_NAME, 
                blob=blob_name
            )
            stream = await blob_client.download_blob()
            return await stream.readall()
    except AzureError as e:
        logger.error(f"Azure Storage Error: {e}")
        raise RuntimeError(f"Could not read blob: {str(e)}")

async def delete_blob(blob_name: str) -> None:
    """Delete a blob from Azure."""
    async with BlobServiceClient.from_connection_string(
        settings.AZURE_STORAGE_CONNECTION_STRING
    ) as client:
        container_client = client.get_container_client(settings.AZURE_BLOB_CONTAINER_NAME)
        await container_client.delete_blob(blob_name, delete_snapshots="include")


def allowed_file(filename: str) -> bool:
    """Updated for Iteration 2: Supports PDF, Images, and Excel."""
    ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "xlsx", "xls", "csv"}
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_sas_url(blob_name: str) -> str:
    """Generates a temporary (15 min) signed URL for private blobs."""
    # This is a synchronous operation, no 'await' needed
    sas_token = generate_blob_sas(
        account_name=settings.AZURE_STORAGE_ACCOUNT_NAME,
        account_key=settings.AZURE_STORAGE_ACCOUNT_KEY,
        container_name=settings.AZURE_BLOB_CONTAINER_NAME,
        blob_name=blob_name,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(minutes=15) 
    )
    
    return f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net/{settings.AZURE_BLOB_CONTAINER_NAME}/{blob_name}?{sas_token}"


def generate_permanent_sas_url(blob_name: str) -> str:
    """Generates a long-lived (5 year) signed URL for images shown in chat history."""
    sas_token = generate_blob_sas(
        account_name=settings.AZURE_STORAGE_ACCOUNT_NAME,
        account_key=settings.AZURE_STORAGE_ACCOUNT_KEY,
        container_name=settings.AZURE_BLOB_CONTAINER_NAME,
        blob_name=blob_name,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(days=365 * 5)
    )
    
    return f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net/{settings.AZURE_BLOB_CONTAINER_NAME}/{quote(blob_name)}?{sas_token}"

def generate_sas_url_with_expiry(blob_name: str, expiry_hours: float = 4.0, container: str = None) -> str:
    """
    Generate a SAS URL for a blob with a configurable expiry window.

    Args:
        blob_name:    The full blob path inside the container.
        expiry_hours: How many hours until the SAS token expires (default 4h).
        container:    Optional container name. Defaults to settings.AZURE_BLOB_CONTAINER_NAME.

    Returns:
        A signed URL string.
    """
    container_name = container or settings.AZURE_BLOB_CONTAINER_NAME
    sas_token = generate_blob_sas(
        account_name=settings.AZURE_STORAGE_ACCOUNT_NAME,
        account_key=settings.AZURE_STORAGE_ACCOUNT_KEY,
        container_name=container_name,
        blob_name=blob_name,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
    )
    if blob_name.__contains__(" "):
        blob_name = quote(blob_name)
    return (
        f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
        f"/{container_name}/{blob_name}?{sas_token}"
    )


async def move_blob(src_blob_name: str, dst_blob_name: str) -> None:
    """
    Copy a blob to a new path then delete the source.
    Used to promote user-uploaded images from temp → permanent storage.

    A short-lived SAS URL is generated for the source so Azure's server-side
    copy works with private containers (bare URLs would be rejected).
    """
    import asyncio

    # Generate a 1-hour source SAS URL for the server-side copy
    src_sas_url = generate_sas_url_with_expiry(src_blob_name, expiry_hours=1.0)

    async with BlobServiceClient.from_connection_string(
        settings.AZURE_STORAGE_CONNECTION_STRING
    ) as blob_service:
        container_client = blob_service.get_container_client(
            settings.AZURE_BLOB_CONTAINER_NAME
        )

        # Server-side copy to destination using the SAS URL
        dst_client = container_client.get_blob_client(dst_blob_name)
        await dst_client.start_copy_from_url(src_sas_url)

        # Wait for copy to finish (usually instant for small blobs)
        while True:
            props = await dst_client.get_blob_properties()
            if props.copy.status == "success":
                break
            if props.copy.status == "failed":
                raise RuntimeError(
                    f"Azure blob copy failed: {props.copy.status_description}"
                )
            await asyncio.sleep(0.2)

        # Delete source
        src_client = container_client.get_blob_client(src_blob_name)
        await src_client.delete_blob()



async def storage_get_all_blobs(prefix: str) -> list:
    """Fetches list of all blob names and their URLs from Azure."""
    blobs = []
    async with BlobServiceClient.from_connection_string(
        settings.AZURE_STORAGE_CONNECTION_STRING
    ) as client:
        container_client = client.get_container_client(settings.AZURE_BLOB_CONTAINER_NAME)
        async for blob in container_client.list_blobs(name_starts_with=prefix):
            blobs.append({
                "name": blob.name,
                "size": blob.size,
                "last_modified": blob.last_modified.isoformat() if blob.last_modified else None,
                "url": f"{container_client.url}/{blob.name}"
            })
    return blobs


async def storage_delete_all_blobs(prefix: str) -> int:
    """Deletes all blobs with the given prefix."""
    count = 0
    async with BlobServiceClient.from_connection_string(
        settings.AZURE_STORAGE_CONNECTION_STRING
    ) as client:
        container_client = client.get_container_client(settings.AZURE_BLOB_CONTAINER_NAME)
        async for blob in container_client.list_blobs(name_starts_with=prefix):
            await container_client.delete_blob(blob.name)
            count += 1
    return count