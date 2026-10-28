import os
import logging
import httpx
from typing import Dict, List
from app.config import settings

logger = logging.getLogger(__name__)

# Constants for Vision (Azure Vision always returns 1024 dimensions)
VISION_DIMENSIONS = 1024

def _base_fields() -> List[Dict]:
    """Common fields shared by both text and image indexes."""
    return [
        {"name": "chunk_id", "type": "Edm.String", "key": True, "filterable": True},
        {"name": "chunk_hash", "type": "Edm.String", "filterable": True},
        {"name": "document_id", "type": "Edm.String", "filterable": True},
        {"name": "source_filename", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "source_type", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "source_document", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "chunk_type", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "content", "type": "Edm.String", "searchable": True},
        {"name": "embedding_text", "type": "Edm.String", "searchable": True},
        {"name": "page_number", "type": "Edm.Int32", "filterable": True, "sortable": True},
        {"name": "section_heading", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "chapter_name", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "topic_name", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "subheading_name", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "topic_id", "type": "Edm.String", "filterable": True},
        
        # ── Clinical Intelligence ───────────────────────────
        {"name": "modality", "type": "Edm.String", "filterable": True},
        {"name": "anatomy", "type": "Collection(Edm.String)", "filterable": True},
        {"name": "visual_signs", "type": "Collection(Edm.String)", "filterable": True},
        {"name": "candidate_conditions", "type": "Collection(Edm.String)", "filterable": True},
        
        # ── Relational Linking ──────────────────────────────
        {"name": "linked_chunk_ids", "type": "Collection(Edm.String)", "filterable": True},

        {"name": "clinical_description", "type": "Edm.String", "searchable": True},
        {"name": "caption", "type": "Edm.String", "searchable": True},
        {"name": "figure_caption", "type": "Edm.String", "searchable": True},
        {"name": "nearby_text", "type": "Edm.String", "searchable": True},
        {"name": "blob_name", "type": "Edm.String", "filterable": True},
        {"name": "figure_id", "type": "Edm.String", "filterable": True},
        {"name": "is_latest", "type": "Edm.Boolean", "filterable": True},
        {"name": "document_version", "type": "Edm.Int32", "filterable": True, "sortable": True},
    ]

def get_text_index_schema(index_name: str, dimensions: int) -> Dict:
    """Schema for text/table index — single content_vector."""
    fields = _base_fields() + [
        {
            "name": "content_vector",
            "type": "Collection(Edm.Single)",
            "searchable": True,
            "retrievable": True,
            "dimensions": dimensions,
            "vectorSearchProfile": "my-vector-profile"
        }
    ]
    return {
        "name": index_name,
        "fields": fields,
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "my-hnsw-config",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine"
                    }
                }
            ],
            "profiles": [
                {
                    "name": "my-vector-profile",
                    "algorithm": "my-hnsw-config"
                }
            ]
        },
        "semantic": {
            "configurations": [
                {
                    "name": "my-semantic-config",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "source_filename"},
                        "prioritizedContentFields": [
                            {"fieldName": "content"}, 
                            {"fieldName": "clinical_description"},
                            {"fieldName": "topic_name"}
                        ],
                        "prioritizedKeywordsFields": [{"fieldName": "caption"}, {"fieldName": "section_heading"}]
                    }
                }
            ]
        }
    }

def get_image_index_schema(index_name: str) -> Dict:
    """Schema for image/figure index — dual vectors (content_vector + image_vector)."""
    fields = _base_fields() + [
        {
            "name": "content_vector",
            "type": "Collection(Edm.Single)",
            "searchable": True,
            "retrievable": True,
            "dimensions": VISION_DIMENSIONS,
            "vectorSearchProfile": "my-vector-profile"
        },
        {
            "name": "image_vector",
            "type": "Collection(Edm.Single)",
            "searchable": True,
            "retrievable": True,
            "dimensions": VISION_DIMENSIONS,
            "vectorSearchProfile": "my-vector-profile"
        }
    ]
    return {
        "name": index_name,
        "fields": fields,
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "my-hnsw-config",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine"
                    }
                }
            ],
            "profiles": [
                {
                    "name": "my-vector-profile",
                    "algorithm": "my-hnsw-config"
                }
            ]
        },
        "semantic": {
            "configurations": [
                {
                    "name": "my-semantic-config",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "source_filename"},
                        "prioritizedContentFields": [{"fieldName": "content"}],
                        "prioritizedKeywordsFields": [{"fieldName": "caption"}, {"fieldName": "section_heading"}]
                    }
                }
            ]
        }
    }

def get_cdt_index_schema(index_name: str) -> Dict:
    """Schema for CDT code index — lightweight, code-focused.

    Uses a DEDICATED semantic configuration ('cdt-semantic-config') separate
    from the dental-book RAG pipeline ('my-semantic-config').  This lets the
    semantic reranker focus entirely on CDT description matching.
    """
    fields = [
        {"name": "code", "type": "Edm.String", "key": True, "filterable": True, "searchable": True},
        {"name": "description", "type": "Edm.String", "searchable": True},
        {"name": "category", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "embedding_text", "type": "Edm.String", "searchable": True},
        {"name": "source_document_id", "type": "Edm.String", "filterable": True},
        {
            "name": "content_vector",
            "type": "Collection(Edm.Single)",
            "searchable": True,
            "retrievable": True,
            "dimensions": VISION_DIMENSIONS,  # 1024-dim — sufficient for short descriptions
            "vectorSearchProfile": "cdt-vector-profile"
        }
    ]
    return {
        "name": index_name,
        "fields": fields,
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "cdt-hnsw-config",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine"
                    }
                }
            ],
            "profiles": [
                {
                    "name": "cdt-vector-profile",
                    "algorithm": "cdt-hnsw-config"
                }
            ]
        },
        "semantic": {
            "configurations": [
                {
                    "name": "cdt-semantic-config",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "code"},
                        "prioritizedContentFields": [
                            {"fieldName": "description"},
                            {"fieldName": "embedding_text"}
                        ],
                        "prioritizedKeywordsFields": [
                            {"fieldName": "category"},
                            {"fieldName": "code"}
                        ]
                    }
                }
            ]
        }
    }

def get_casestudy_index_schema(index_name: str, dimensions: int) -> Dict:
    """Schema for case study index — clinical case entries with content_vector."""
    fields = [
        {"name": "case_id", "type": "Edm.String", "key": True, "filterable": True, "searchable": True},
        {"name": "title", "type": "Edm.String", "searchable": True},
        {"name": "content", "type": "Edm.String", "searchable": True},
        {"name": "embedding_text", "type": "Edm.String", "searchable": True},
        {
            "name": "content_vector",
            "type": "Collection(Edm.Single)",
            "searchable": True,
            "retrievable": True,
            "dimensions": dimensions,
            "vectorSearchProfile": "casestudy-vector-profile"
        }
    ]
    return {
        "name": index_name,
        "fields": fields,
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "casestudy-hnsw-config",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine"
                    }
                }
            ],
            "profiles": [
                {
                    "name": "casestudy-vector-profile",
                    "algorithm": "casestudy-hnsw-config"
                }
            ]
        },
        "semantic": {
            "configurations": [
                {
                    "name": "casestudy-semantic-config",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [
                            {"fieldName": "content"},
                            {"fieldName": "embedding_text"}
                        ],
                        "prioritizedKeywordsFields": [
                            {"fieldName": "title"}
                        ]
                    }
                }
            ]
        }
    }

async def ensure_search_index_exists(index_name: str) -> bool:
    """
    Checks if an index exists in Azure AI Search. 
    If it doesn't, creates it with the default schema.
    """
    endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
    api_key = settings.AZURE_SEARCH_KEY
    url = f"{endpoint}/indexes/{index_name}?api-version=2024-07-01"
    
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        # 1. Check if index exists
        check_resp = await client.get(url, headers=headers)
        if check_resp.status_code == 200:
            return True # Already exists

        if check_resp.status_code != 404:
            logger.error(f"[SEARCH] Error checking index {index_name}: {check_resp.status_code}")
            return False

        # 2. Not found -- Create it
        logger.info(f"[SEARCH] Index '{index_name}' not found. Creating it now...")
        
        if index_name == settings.AZURE_SEARCH_INDEX_TEXT:
            schema = get_text_index_schema(index_name, settings.OPENAI_EMBEDDING_DIMENSIONS)
        elif index_name == settings.AZURE_SEARCH_INDEX_IMAGE:
            schema = get_image_index_schema(index_name)
        elif index_name == settings.AZURE_SEARCH_INDEX_CDT:
            schema = get_cdt_index_schema(index_name)
        elif index_name == settings.AZURE_SEARCH_INDEX_CASE_STUDY:
            schema = get_casestudy_index_schema(index_name, settings.OPENAI_EMBEDDING_DIMENSIONS)
        else:
            logger.error(f"[SEARCH] Unknown index name: {index_name}")
            return False

        create_resp = await client.put(url, headers=headers, json=schema)
        if create_resp.status_code in (200, 201, 204):
            logger.info(f"[SEARCH] Successfully created index '{index_name}'")
            return True
        else:
            logger.error(f"[SEARCH] Failed to create index '{index_name}': {create_resp.status_code} - {create_resp.text}")
            return False
