import os
import httpx
from dotenv import load_dotenv

load_dotenv()

# We can query Azure AI Search REST API directly for `$count`
endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")
key = os.getenv("AZURE_SEARCH_KEY")
headers = {"api-key": key, "Content-Type": "application/json"}

for index in [os.getenv("AZURE_SEARCH_INDEX_TEXT"), os.getenv("AZURE_SEARCH_INDEX_IMAGE")]:
    url = f"{endpoint}/indexes/{index}/docs/$count?api-version=2024-07-01"
    try:
        res = httpx.get(url, headers=headers)
        res.raise_for_status()
        print(f"✅ {index}: {res.text} chunks saved in Azure cloud")
    except Exception as e:
        print(f"❌ {index} failed: {e} - {res.text if 'res' in locals() else ''}")
