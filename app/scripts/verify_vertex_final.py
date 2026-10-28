import asyncio
import os
from app.config import settings
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

async def verify_final_integration():
    print("--- Final Integration Verification ---")
    print(f"Model: {settings.GEMINI_CHAT_MODEL}")
    print(f"Project: {settings.GOOGLE_CLOUD_PROJECT}")
    print(f"Location: {settings.GOOGLE_CLOUD_LOCATION}")
    
    creds = settings.get_vertex_credentials()
    if creds:
        print("[SUCCESS] Credentials loaded correctly from config.")
    else:
        print("[FAILURE] Could not load credentials from gcp-credentials.json.")
        return

    try:
        llm = ChatGoogleGenerativeAI(
            model=settings.GEMINI_CHAT_MODEL,
            project=settings.GOOGLE_CLOUD_PROJECT,
            location=settings.GOOGLE_CLOUD_LOCATION,
            credentials=creds,
            vertexai=True,
        )
        print("Invoking model...")
        resp = await llm.ainvoke([HumanMessage(content="Hello, are you there?")])
        print(f"Response: {resp.content}")
        print("\n[SUCCESS] VERTEX AI INTEGRATION IS COMPLETE AND WORKING!")
    except Exception as e:
        print(f"\n[FAILURE] Final verification failed: {e}")

if __name__ == "__main__":
    asyncio.run(verify_final_integration())
