"""
Azure AI Search Initialization Script

This script programmatically configures and creates the required Vector Search Indexes 
(`dental-text-index` and `dental-image-index`) directly in your Azure account.

What this script accomplishes:
1. Connects to the Azure AI Search instance defined in your `.env`.
2. Defines the schema with metadata fields for text and image indexes.
3. `dental-text-index`: 3072-dim content_vector (OpenAI text-embedding-3-large).
4. `dental-image-index`: 1024-dim content_vector (text) + 1024-dim image_vector (Azure Vision).
5. Enables HNSW algorithm for fast semantic retrieval.
6. Includes `embedding_text` (searchable) and `figure_id` (filterable) for multimodal search.

Execute this script ONCE when spinning up a new environment or changing the vector schema.
"""

import os
import sys
import asyncio
import httpx
from dotenv import load_dotenv

# Load the environment variables from .env
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"))

AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY", "")
AZURE_SEARCH_INDEX_TEXT = os.getenv("AZURE_SEARCH_INDEX_TEXT", "dental-text-index")
AZURE_SEARCH_INDEX_IMAGE = os.getenv("AZURE_SEARCH_INDEX_IMAGE", "dental-image-index")
OPENAI_DIMENSIONS = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", 3072))
VISION_DIMENSIONS = 1024  # Azure Vision always returns 1024 dimensions

if not AZURE_SEARCH_ENDPOINT or not AZURE_SEARCH_KEY:
    print("[ERROR] AZURE_SEARCH_ENDPOINT or AZURE_SEARCH_KEY missing from .env")
    sys.exit(1)


def _base_fields() -> list:
    return [
        {"name": "chunk_id", "type": "Edm.String", "key": True, "filterable": True},
        {"name": "document_id", "type": "Edm.String", "filterable": True},
        {"name": "source_type", "type": "Edm.String", "filterable": True},
        {"name": "source_filename", "type": "Edm.String", "filterable": True},
        {"name": "chunk_type", "type": "Edm.String", "filterable": True},
        {"name": "content", "type": "Edm.String", "searchable": True},
        {"name": "embedding_text", "type": "Edm.String", "searchable": True},
        {"name": "page_number", "type": "Edm.Int32", "filterable": True, "sortable": True},
        
        # ── Hierarchical Metadata ────────────────────────────────────────────────
        {"name": "chapter_name", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "topic_name", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "subheading_name", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "section_heading", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "topic_id", "type": "Edm.String", "filterable": True},

        # ── Visual/Image Metadata ────────────────────────────────────────────────
        {"name": "caption", "type": "Edm.String", "searchable": True},
        {"name": "figure_caption", "type": "Edm.String", "searchable": True},
        {"name": "nearby_text", "type": "Edm.String", "searchable": True},
        {"name": "clinical_description", "type": "Edm.String", "searchable": True},
        {"name": "blob_name", "type": "Edm.String", "filterable": True},
        {"name": "figure_id", "type": "Edm.String", "filterable": True},

        # ── Clinical Intelligence Vectors ─────────────────────────────────────────
        {"name": "modality", "type": "Edm.String", "filterable": True, "searchable": True},
        {"name": "anatomy", "type": "Collection(Edm.String)", "filterable": True, "searchable": True},
        {"name": "visual_signs", "type": "Collection(Edm.String)", "filterable": True, "searchable": True},
        {"name": "candidate_conditions", "type": "Collection(Edm.String)", "filterable": True, "searchable": True},
        {"name": "linked_chunk_ids", "type": "Collection(Edm.String)", "filterable": True},

        {"name": "is_latest", "type": "Edm.Boolean", "filterable": True},
        {"name": "document_version", "type": "Edm.Int32", "filterable": True, "sortable": True},
    ]


def _semantic_config() -> dict:
    """Shared semantic search configuration for reranking."""
    return {
        "defaultConfiguration": "my-semantic-config",
        "configurations": [
            {
                "name": "my-semantic-config",
                "prioritizedFields": {
                    "prioritizedContentFields": [
                        {"fieldName": "content"},
                        {"fieldName": "embedding_text"},
                    ],
                    "titleField": {"fieldName": "section_heading"},
                    "prioritizedKeywordsFields": [
                        {"fieldName": "caption"},
                    ],
                },
            }
        ],
    }


def build_text_schema(index_name: str, dimensions: int) -> dict:
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
                        "prioritizedContentFields": [{"fieldName": "content"}],
                        "prioritizedKeywordsFields": [{"fieldName": "caption"}, {"fieldName": "section_heading"}]
                    }
                }
            ]
        }
    }


def build_image_schema(index_name: str) -> dict:
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


async def create_index(client: httpx.AsyncClient, index_name: str, schema: dict):
    url = f"{AZURE_SEARCH_ENDPOINT}/indexes/{index_name}?api-version=2024-07-01"
    
    # 1. In-place update: We skip deletion to preserve existing chunks!
    # delete_response = await client.delete(url)
    # if delete_response.status_code in (200, 204):
    #     print(f"🗑️ Deleted existing index '{index_name}'")
    
    # 2. Create the index
    response = await client.put(url, json=schema)
    if response.status_code in (200, 201, 204):
        print(f"[OK] Updated '{index_name}' schema")
    else:
        print(f"[ERROR] Failed to update '{index_name}': {response.status_code}")
        print(response.text)


async def main():
    print(f"[SEARCH] Connecting to Azure AI Search: {AZURE_SEARCH_ENDPOINT}")
    async with httpx.AsyncClient(headers={"api-key": AZURE_SEARCH_KEY, "Content-Type": "application/json"}) as client:
        # Text Index (3072-dim content_vector)
        text_schema = build_text_schema(AZURE_SEARCH_INDEX_TEXT, OPENAI_DIMENSIONS)
        await create_index(client, AZURE_SEARCH_INDEX_TEXT, text_schema)
        
        # Image Index (1024-dim content_vector + 1024-dim image_vector)
        image_schema = build_image_schema(AZURE_SEARCH_INDEX_IMAGE)
        await create_index(client, AZURE_SEARCH_INDEX_IMAGE, image_schema)

    print("\n[INFO] Text index fields:")
    for f in text_schema["fields"]:
        extra = f" ({f['dimensions']}d)" if "dimensions" in f else ""
        print(f"   {f['name']}: {f['type']}{extra}")
    
    print(f"\n[INFO] Image index fields:")
    for f in image_schema["fields"]:
        extra = f" ({f['dimensions']}d)" if "dimensions" in f else ""
        print(f"   {f['name']}: {f['type']}{extra}")


if __name__ == "__main__":
    asyncio.run(main())
