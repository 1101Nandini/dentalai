from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from app.utils.blob_utils import download_blob
from app.utils.file_utils import generate_sas_url
from app.config import settings
import logging
import urllib.parse
import io
import mimetypes

router = APIRouter()
logger = logging.getLogger(__name__)

def parse_blob_url(blob_url: str):
    """
    Parses an Azure Blob URL to extract container and blob name.
    Example: https://account.blob.core.windows.net/container/path/to/blob.ext
    """
    parsed = urllib.parse.urlparse(blob_url)
    path = parsed.path.lstrip('/')
    parts = path.split('/', 1)
    if len(parts) < 2:
        return None, None
    container = parts[0]
    blob_name = parts[1]
    return container, blob_name

@router.get("/proxy")
async def blob_proxy(
    blob_url: str = Query(..., description="The full URL of the Azure Blob"),
    return_type: str = Query(..., regex="^(object|sas)$", description="Type of return: 'object' for content, 'sas' for signed URL")
):
    """
    Proxy route for Azure Blob files.
    Returns either the signed SAS URL or the blob content itself.
    """
    try:
       
        container, blob_name = parse_blob_url(blob_url)
        if not container or not blob_name:
            raise HTTPException(status_code=400, detail="Invalid blob_url format")

        # For SAS return type
        if return_type == "sas":
            # Note: generate_sas_url in file_utils.py currently uses settings.AZURE_BLOB_CONTAINER_NAME
            # We might want to use the container from the URL if it's different.
            # However, file_utils.py doesn't currently support passing a container name to generate_sas_url easily
            # without modifying it. Let's see if we can use generate_sas_url_with_expiry instead.
            from app.utils.file_utils import generate_sas_url_with_expiry
            sas_url = generate_sas_url_with_expiry(blob_name, container=container)
            return {
                "success": True,
                "message": "SAS URL generated successfully",
                "data": {"sas_url": sas_url}
            }

        # For object return type
        if return_type == "object":
            # We need a version of download_blob that takes a container name
            # Let's check blob_utils.py again.
            from azure.storage.blob.aio import BlobServiceClient
            
            async with BlobServiceClient.from_connection_string(
                settings.AZURE_STORAGE_CONNECTION_STRING
            ) as client:
                container_client = client.get_container_client(container)
                blob_client = container_client.get_blob_client(blob_name)
                
                try:
                    stream = await blob_client.download_blob()
                    content = await stream.readall()
                    
                    # Guess content type
                    content_type, _ = mimetypes.guess_type(blob_name)
                    if not content_type:
                        content_type = "application/octet-stream"
                        
                    return Response(
                        content=content,
                        media_type=content_type,
                        headers={"Content-Disposition": f'inline; filename="{blob_name.split("/")[-1]}"'}
                    )
                except Exception as e:
                    logger.error(f"Failed to download blob: {e}")
                    raise HTTPException(status_code=404, detail=f"Blob not found or inaccessible: {str(e)}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in blob proxy: {e}")
        raise HTTPException(status_code=500, detail=str(e))
