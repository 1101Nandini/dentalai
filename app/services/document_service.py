"""
DocumentService — upload, list, get, delete documents.

Upload design (single file per call):
  1. Read file bytes.
  2. Re-check file_hash against DB (race-condition guard).
  3. Upload blob to Azure.
  4. insert_one with file_hash and file_type at top level.
  5. Return { id: mongo _id, ... }  — frontend uuid is never stored.
"""
import uuid as _uuid_mod
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from bson import ObjectId
from fastapi import HTTPException, UploadFile, status
import httpx

from app.config import settings

from app.core.database import get_database
from app.utils.file_utils import generate_sas_url, upload_blob, delete_blob
from app.utils.file_utils import storage_get_all_blobs, storage_delete_all_blobs


class DocumentService:

    @staticmethod
    async def upload_document(
        file: UploadFile,
        file_hash: str,
        metadata: Dict[str, Any],
    ) -> dict:
        """
        Upload a single document.

        - file_hash and file_type are stored at top level for fast indexing.
        - metadata is stored as-is (includes Image Name, Description, etc. for XLSX-backed images).
        - Frontend uuid is NOT accepted here — never stored.
        - Versioning: If metadata contains 'replaces_document_id', a version chain is created.
        """
        db = await get_database()

        # ── Race-condition guard: re-check hash right before insert ────────────
        existing = await db["documents"].find_one(
            {"file_hash": file_hash},
            {"_id": 1}
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Duplicate file — this exact file has already been uploaded.",
                    "duplicate_id": str(existing["_id"]),
                    "duplicate_type": "hash_match",
                }
            )

        # ── Read & upload ──────────────────────────────────────────────────────
        import time as _time
        t0 = _time.perf_counter()

        content = await file.read()
        t_read = _time.perf_counter() - t0
        safe_filename = file.filename.encode('ascii', 'ignore').decode()
        print(f"[UPLOAD] [SERVER_UPLOAD] Filename: {safe_filename}, Size: {len(content)} bytes (read: {t_read:.1f}s)")

        unique_prefix = str(_uuid_mod.uuid4())[:8]
        blob_name = f"admin_doc/{unique_prefix}_{file.filename}"

        t_blob = _time.perf_counter()
        await upload_blob(blob_name, content, file.content_type or "application/octet-stream")
        blob_elapsed = _time.perf_counter() - t_blob
        print(f"  [BLOB] [BLOB_UPLOAD] {len(content)/(1024*1024):.1f} MB -> Azure in {blob_elapsed:.1f}s")

        # ── Determine version lineage ──────────────────────────────────────────
        file_type = metadata.get("file_type") or file.content_type or "application/octet-stream"
        replaces_doc_id = metadata.pop("replaces_document_id", None)
        document_version = 1
        parent_document_id = None

        if replaces_doc_id:
            parent_doc = await db["documents"].find_one(
                {"_id": ObjectId(replaces_doc_id)},
                {"document_version": 1, "document_title": 1, "author": 1}
            )
            if parent_doc:
                document_version = (parent_doc.get("document_version") or 1) + 1
                parent_document_id = replaces_doc_id
                # Copy title/author from parent if not provided in metadata
                if not metadata.get("document_title") and parent_doc.get("document_title"):
                    metadata["document_title"] = parent_doc["document_title"]
                if not metadata.get("author") and parent_doc.get("author"):
                    metadata["author"] = parent_doc["author"]

        # ── Build document ─────────────────────────────────────────────────────
        import os
        now = datetime.now(timezone.utc)
        doc_entry = {
            "original_filename": os.path.basename(file.filename),
            "file_hash": file_hash,          # top-level for unique index
            "file_type": file_type,           # top-level for fast filtering
            "blob_name": blob_name,
            "status": "saved",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
            "metadata": metadata,
            # ── Versioning fields ──────────────────────────────────────────────
            "document_title": metadata.get("document_title"),
            "author": metadata.get("author"),
            "document_version": document_version,
            "parent_document_id": parent_document_id,
            "replaced_by": None,
            "version_iteration": 0,
            "is_latest": True,
        }

        t_db = _time.perf_counter()
        result = await db["documents"].insert_one(doc_entry)
        db_elapsed = _time.perf_counter() - t_db

        # ── Update parent with back-reference ──────────────────────────────────
        if parent_document_id:
            await db["documents"].update_one(
                {"_id": ObjectId(parent_document_id)},
                {"$set": {
                    "replaced_by": str(result.inserted_id),
                    "is_latest": False
                }}
            )
            print(f"  [VERSION] Created V{document_version} (replaces {parent_document_id[:8]})")

        total_elapsed = _time.perf_counter() - t0
        print(f"  DB_INSERT in {db_elapsed:.1f}s | Total upload: {total_elapsed:.1f}s")

        doc_entry["id"] = str(result.inserted_id)
        doc_entry["secure_url"] = generate_sas_url(blob_name)
        doc_entry.pop("_id", None)

        return doc_entry

    # ── Legacy batch upload kept for backwards compat (used by old route) ──────
    @staticmethod
    async def upload_documents(files: List[UploadFile], metadata_list: List[Dict[str, Any]]) -> dict:
        """
        Legacy: upload multiple files without dedup hashes.
        Kept for backwards compatibility — prefer upload_document() for new uploads.
        """
        db = await get_database()
        uploaded_docs = []

        for i, file in enumerate(files):
            meta = metadata_list[i] if i < len(metadata_list) else {}
            unique_id = str(_uuid_mod.uuid4())[:8]
            blob_name = f"admin_doc/{unique_id}_{file.filename}"

            try:
                content = await file.read()
                await upload_blob(blob_name, content, file.content_type)

                now = datetime.now(timezone.utc)
                doc_entry = {
                    "original_filename": file.filename,
                    "blob_name": blob_name,
                    "status": "saved",
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                    "metadata": meta,
                }

                result = await db["documents"].insert_one(doc_entry)
                doc_entry["id"] = str(result.inserted_id)
                doc_entry["secure_url"] = generate_sas_url(blob_name)
                doc_entry.pop("_id", None)
                uploaded_docs.append(doc_entry)

            except Exception as e:
                print(f"Error processing {file.filename}: {str(e)}")
                continue

        return {"count": len(uploaded_docs), "documents": uploaded_docs}

    @staticmethod
    async def list_documents(skip: int, limit: int, search: Optional[str] = None) -> dict:
        """Admin: Retrieve all documents with total count and secure URLs."""
        db = await get_database()
        
        query = {}
        if search:
            regex = {"$regex": search, "$options": "i"}
            query["$or"] = [
                {"original_filename": regex},
                {"document_title": regex},
                {"author": regex},
                {"metadata.Image Name": regex},
                {"metadata.Description": regex}
            ]
            
        total_count = await db["documents"].count_documents(query)
        cursor = db["documents"].find(query, {"parsed_elements": 0}).sort("created_at", -1).skip(skip).limit(limit).allow_disk_use(True)

        docs = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            doc["secure_url"] = generate_sas_url(doc["blob_name"])
            doc.pop("blob_url", None)
            docs.append(doc)

        return {"total": total_count, "documents": docs}

    @staticmethod
    async def get_document(document_id: str) -> dict:
        """Admin: Get a single document with a fresh secure URL."""
        db = await get_database()
        if not ObjectId.is_valid(document_id):
            raise HTTPException(status_code=400, detail="Invalid ID format")

        doc = await db["documents"].find_one({"_id": ObjectId(document_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        doc["id"] = str(doc.pop("_id"))
        doc["secure_url"] = generate_sas_url(doc["blob_name"])
        doc.pop("blob_url", None)
        return doc

    @staticmethod
    async def merge_document_versions(document_id: str) -> dict:
        """
        Admin API: Approve semantic overlaps for a document.
        Sets is_latest=False for the older 'duplicate_of' chunks in Azure AI Search and MongoDB.
        Clears the semantic_overlaps list on the document.
        """
        db = await get_database()
        if not ObjectId.is_valid(document_id):
            raise HTTPException(status_code=400, detail="Invalid Document ID format")
            
        doc = await db["documents"].find_one({"_id": ObjectId(document_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
            
        overlaps = doc.get("semantic_overlaps", [])
        if not overlaps:
            return {"message": "No semantic overlaps found to merge", "merged_count": 0}
            
        # ── Update MongoDB for old chunks ──────────────────────────────────────
        old_chunk_ids = [o["duplicate_of"] for o in overlaps]
        
        await db["chunks"].update_many(
            {"chunk_id": {"$in": old_chunk_ids}},
            {"$set": {"is_latest": False}}
        )
        
        # ── Update Azure AI Search for old chunks ──────────────────────────────
        import httpx
        from app.config import settings
        
        search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
        search_key = settings.AZURE_SEARCH_KEY
        api_version = "2024-07-01"
        
        old_chunks = await db["chunks"].find({"chunk_id": {"$in": old_chunk_ids}}).to_list(length=None)
        
        text_payloads = []
        image_payloads = []
        TEXT_CHUNK_TYPES = {"text", "table"}
        IMAGE_CHUNK_TYPES = {"image", "figure", "xray"}
        
        for c in old_chunks:
            payload = {
                "@search.action": "merge",
                "chunk_id": c["chunk_id"],
                "is_latest": False
            }
            if c.get("chunk_type") in TEXT_CHUNK_TYPES:
                text_payloads.append(payload)
            elif c.get("chunk_type") in IMAGE_CHUNK_TYPES:
                image_payloads.append(payload)
                
        async def push_merge(index_name, payloads):
            if not payloads:
                return
            url = f"{search_endpoint}/indexes/{index_name}/docs/index?api-version={api_version}"
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                response = await http_client.post(
                    url,
                    headers={"Content-Type": "application/json", "api-key": search_key},
                    json={"value": payloads}
                )
                if response.status_code not in (200, 207):
                    print(f"Error updating Azure AI Search is_latest=False for index {index_name}")
                    
        await push_merge(settings.AZURE_SEARCH_INDEX_TEXT, text_payloads)
        await push_merge(settings.AZURE_SEARCH_INDEX_IMAGE, image_payloads)
        
        # ── Clear overlaps and update document version ─────────────────────────
        await db["documents"].update_one(
            {"_id": ObjectId(document_id)},
            {
                "$set": {"semantic_overlaps": []},
                "$inc": {"version_iteration": 1}
            }
        )
        
        return {
            "message": "Successfully merged versions. Old chunks deprecated.", 
            "merged_count": len(old_chunk_ids)
        }

    @staticmethod
    async def delete_document(document_id: str) -> None:
        db = await get_database()
        if not ObjectId.is_valid(document_id):
            raise HTTPException(status_code=400, detail="Invalid Document ID format")

        oid = ObjectId(document_id)
        doc = await db["documents"].find_one({"_id": oid})

        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        # 1. Gather all associated chunks
        chunks_cursor = db["chunks"].find(
            {"document_id": str(oid)},
            {"chunk_id": 1, "chunk_type": 1, "is_indexed": 1}
        )
        text_chunk_ids = []
        image_chunk_ids = []

        async for chunk in chunks_cursor:
            # We only need to delete from Azure Search if it was indexed
            if chunk.get("is_indexed"):
                if chunk.get("chunk_type") in ("image", "figure"):
                    image_chunk_ids.append(chunk["chunk_id"])
                else:
                    text_chunk_ids.append(chunk["chunk_id"])

        # 2. Delete from Azure AI Search
        if text_chunk_ids or image_chunk_ids:
            search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
            api_key = settings.AZURE_SEARCH_KEY
            api_version = "2024-07-01"

            async def _delete_from_index(index_name: str, c_ids: list[str]):
                if not c_ids: return
                batch = [{"@search.action": "delete", "chunk_id": cid} for cid in c_ids]
                url = f"{search_endpoint}/indexes/{index_name}/docs/index?api-version={api_version}"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    try:
                        await client.post(url, headers={"api-key": api_key, "Content-Type": "application/json"}, json={"value": batch})
                    except Exception as e:
                        print(f"Failed to delete {len(c_ids)} chunks from {index_name}: {e}")

            await _delete_from_index(settings.AZURE_SEARCH_INDEX_TEXT, text_chunk_ids)
            await _delete_from_index(settings.AZURE_SEARCH_INDEX_IMAGE, image_chunk_ids)

        # 3. Delete chunks from MongoDB
        await db["chunks"].delete_many({"document_id": str(oid)})

        # 4. Delete blob from Azure Storage
        await delete_blob(doc["blob_name"])

        # 5. Delete document record
        await db["documents"].delete_one({"_id": oid})

        # 6. Fallback Logic: If we deleted the "latest" version, we must promote the previous version!
        if doc.get("is_latest") and doc.get("document_title") and doc.get("author"):
            previous_latest = await db["documents"].find_one(
                {
                    "document_title": doc["document_title"],
                    "author": doc["author"],
                    "_id": {"$ne": oid}
                },
                sort=[("document_version", -1)]
            )
            if previous_latest:
                prev_id = previous_latest["_id"]
                # Promote to Latest in Mongo
                await db["documents"].update_one(
                    {"_id": prev_id},
                    {"$set": {"is_latest": True, "replaced_by": None}}
                )
                
                # Fetch all chunks for the previous version to update Azure Search
                prev_chunks_cursor = db["chunks"].find(
                    {"document_id": str(prev_id), "is_indexed": True},
                    {"chunk_id": 1, "chunk_type": 1}
                )
                prev_text_chunks = []
                prev_image_chunks = []
                async for pc in prev_chunks_cursor:
                    if pc.get("chunk_type") in ("image", "figure"):
                        prev_image_chunks.append(pc["chunk_id"])
                    else:
                        prev_text_chunks.append(pc["chunk_id"])

                search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
                api_key = settings.AZURE_SEARCH_KEY
                api_version = "2024-07-01"

                async def _update_is_latest(index_name: str, c_ids: list[str]):
                    if not c_ids: return
                    # "@search.action": "merge" patches existing fields without deleting them
                    batch = [{"@search.action": "merge", "chunk_id": cid, "is_latest": True} for cid in c_ids]
                    url = f"{search_endpoint}/indexes/{index_name}/docs/index?api-version={api_version}"
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        try:
                            await client.post(url, headers={"api-key": api_key, "Content-Type": "application/json"}, json={"value": batch})
                        except Exception as e:
                            print(f"[ERROR] Failed to promote V{previous_latest.get('document_version')} chunks in {index_name}: {e}")

                await _update_is_latest(settings.AZURE_SEARCH_INDEX_TEXT, prev_text_chunks)
                await _update_is_latest(settings.AZURE_SEARCH_INDEX_IMAGE, prev_image_chunks)
                print(f"[DEDUP] Deleted V{doc.get('document_version')}. Successfully promoted V{previous_latest.get('document_version')} to is_latest=True.")

    @staticmethod
    async def manage_storage_directly(action: str) -> dict:
        """Dev Tool: Directly interact with Azure Blob admin_doc folder."""
        prefix = "admin_doc/"

        if action == "fetch":
            files = await storage_get_all_blobs(prefix)
            return {"folder": prefix, "count": len(files), "files": files}

        elif action == "delete":
            deleted_count = await storage_delete_all_blobs(prefix)
            return {"folder": prefix, "deleted_count": deleted_count, "message": "Storage cleared successfully"}

        else:
            raise HTTPException(status_code=400, detail="Invalid action. Use 'fetch' or 'delete'.")