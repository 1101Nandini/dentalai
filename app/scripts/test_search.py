import os
import sys
import asyncio
import httpx
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

# Load .env
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"))

AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY", "")
AZURE_SEARCH_INDEX_TEXT = os.getenv("AZURE_SEARCH_INDEX_TEXT", "dental-text-index")
AZURE_SEARCH_INDEX_IMAGE = os.getenv("AZURE_SEARCH_INDEX_IMAGE", "dental-image-index")

async def embed_query(query: str, dimensions: int):
    client = AsyncAzureOpenAI(
        azure_endpoint=os.getenv("AZURE_OPENAI_EMBEDDING_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_EMBEDDING_KEY"),
        api_version="2023-05-15"
    )
    response = await client.embeddings.create(
        input=[query],
        model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"),
        dimensions=dimensions
    )
    return response.data[0].embedding

async def search_index(query: str, index_name: str = None):
    if not index_name:
        index_name = AZURE_SEARCH_INDEX_TEXT
    
    dims = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", 3072))
    if index_name == AZURE_SEARCH_INDEX_IMAGE:
        dims = 1024 # Standardized for Vision index

    print(f"🔍 Embedding your query: '{query}' ({dims} dims) in index '{index_name}'")
    vector = await embed_query(query, dims)
    
    url = f"{AZURE_SEARCH_ENDPOINT}/indexes/{index_name}/docs/search?api-version=2024-07-01"
    
    # Dynamic fields based on index
    select_fields = "chunk_id, content, section_heading, source_filename, page_number"
    if index_name == AZURE_SEARCH_INDEX_IMAGE:
        select_fields = "chunk_id, content, section_heading, source_filename, blob_name, caption"

    payload = {
        "vectorQueries": [
            {
                "kind": "vector",
                "vector": vector,
                "exhaustive": True,
                "fields": "content_vector",
                "k": 3
            }
        ],
        "select": select_fields,
        "top": 3
    }

    print("⚡ Hitting Azure AI Search...")
    async with httpx.AsyncClient(headers={"api-key": AZURE_SEARCH_KEY, "Content-Type": "application/json"}) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        
        results = response.json().get("value", [])
        
        print(f"\n✅ Found {len(results)} exact semantic matches!\n")
        
        for idx, doc in enumerate(results, 1):
            score = doc.get("@search.score", 0)
            print(f"--- MATCH #{idx} (Score: {score:.3f}) ---")
            print(f"📄 File: {doc.get('source_filename')} (Page {doc.get('page_number', 'N/A')})")
            if 'blob_name' in doc: print(f"🔗 Blob: {doc['blob_name']}")
            if 'caption' in doc: print(f"💬 Caption: {doc['caption']}")
            print(f"📌 Section: {doc.get('section_heading')}")
            print(f"📝 Content: {doc.get('content', '')}\n")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_search.py \"Your search query here\" [index_name]")
        sys.exit(1)
        
    query = sys.argv[1]
    index = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(search_index(query, index))
