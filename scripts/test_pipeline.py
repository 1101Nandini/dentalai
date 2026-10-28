#!/usr/bin/env python
"""
Complete Pipeline Test Script
Runs the entire audio processing, transcription, CDT lookup, session saving,
and SOAP generation flow.
"""
import sys
import os
import asyncio
from bson import ObjectId

# Add project root to python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.core.database import get_database, connect_to_mongo, close_mongo_connection
from app.services.voice_stream_service import VoiceStreamService

async def run_pipeline(
    audio_url: str,
    user_id: str,
    org_id: str,
    session_name: str = "Test Pipeline Session",
    template_id: str = None
):
    await connect_to_mongo()
    try:
        print("=" * 60)
        print("DENTAL VOICE AUDIO PROCESSING PIPELINE TEST")
        print("=" * 60)
        print(f"Audio URL:       {audio_url.split('?')[0]}")
        print(f"User ID:         {user_id}")
        print(f"Organization ID: {org_id}")
        print(f"Session Name:    {session_name}")
        print(f"Template ID:     {template_id}")
        print("-" * 60)

        # 1. Initialize VoiceStreamService
        print("[1/5] Initializing VoiceStreamService...")
        session = VoiceStreamService(user_id=user_id, organization_id=org_id)
        
        # Extract a dummy/real blob name path from the URL
        if "audio_sessions" in audio_url:
            blob_path = audio_url.split("?")[0].split("/audio_sessions/")[-1]
            session.audio_blob_name = f"audio_sessions/{blob_path}"
        else:
            session.audio_blob_name = f"audio_sessions/{user_id}/test_stream_{session.stream_id}.webm"
        
        # Regenerate a fresh SAS URL to avoid authentication/expiration issues
        from app.utils.file_utils import generate_sas_url
        fresh_audio_url = generate_sas_url(session.audio_blob_name)
        
        # 2. Transcription via Gemini STT (with built-in Ogg conversion)
        print("[2/5] Transcribing audio via Gemini STT (and Ogg conversion)...")
        try:
            raw_transcript = await session._call_gemini_stt(fresh_audio_url)
            session.raw_transcript = raw_transcript
            
            # Apply standard Whisper/filler cleanup rules if any
            from app.services.voice_stream_service import _clean_whisper_hallucinations
            session.transcript = _clean_whisper_hallucinations(raw_transcript)
            
            print("\n--- Cleaned Transcript ---")
            print(session.transcript)
            print("-" * 40)
        except Exception as e:
            print(f"ERROR during transcription: {e}")
            return

        # 3. CDT Code Lookup
        print("[3/5] Performing CDT Code Lookup...")
        try:
            cdt_result = await session.lookup_cdt()
            print(f"CDT Detection Mode: {cdt_result.get('mode')}")
            print("Detected Entries:")
            for idx, entry in enumerate(cdt_result.get("entries", []), 1):
                print(f"  {idx}. CDT Code: {entry.get('cdt_code')} | Procedure: {entry.get('procedure')} | Tooth: {entry.get('tooth')}")
        except Exception as e:
            print(f"ERROR during CDT lookup: {e}")
            return

        # 4. Save Session to DB
        print("[4/5] Saving session to database...")
        try:
            session_id = await session.save_session(
                cdt_result=cdt_result,
                session_name=session_name,
                template_id=template_id
            )
            print(f"Session saved successfully. Session ID: {session_id}")
        except Exception as e:
            print(f"ERROR saving session: {e}")
            return

        # 5. Generate SOAP Note (Synchronous execution of background task)
        print("[5/5] Generating SOAP Note and Patient Summary...")
        try:
            await VoiceStreamService.run_soap_background(
                session_id=session_id,
                transcript=session.transcript,
                user_id=user_id,
                template_id=template_id,
                org_id=org_id
            )
            print("SOAP Note generation task completed.")
        except Exception as e:
            print(f"ERROR generating SOAP note: {e}")
            return

        # 6. Fetch and print final results from database
        print("-" * 60)
        print("PIPELINE EXECUTION COMPLETE. FINAL DOCUMENT FROM DB:")
        print("-" * 60)
        
        db = await get_database()
        doc = await db["dental_voice_sessions"].find_one({"_id": ObjectId(session_id)})
        
        if not doc:
            print("ERROR: Could not find saved session document in the database.")
            return
            
        print(f"Status:          {doc.get('status')}")
        print(f"SOAP Note:")
        print("=" * 60)
        print(doc.get("soap_note") or "(empty)")
        print("=" * 60)
        print(f"Patient Summary:")
        print("~" * 60)
        print(doc.get("patient_summary") or "(empty)")
        print("~" * 60)
        print(f"Extracted CDT Entries (Completed Today):")
        print(doc.get("completed_today"))
        print(f"Extracted CDT Entries (Planned Future):")
        print(doc.get("planned_future"))
        print("=" * 60)
    finally:
        await close_mongo_connection()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test full dental voice pipeline.")
    parser.add_argument("--url", type=str, required=True, help="SAS URL of the clinical audio file")
    parser.add_argument("--user-id", type=str, required=True, help="User ID to associate session with")
    parser.add_argument("--org-id", type=str, required=True, help="Organization ID")
    parser.add_argument("--name", type=str, default="Test Pipeline Run", help="Session Name")
    parser.add_argument("--template-id", type=str, default=None, help="Ghostwriter Template ID (optional)")

    args = parser.parse_args()

    asyncio.run(
        run_pipeline(
            audio_url=args.url,
            user_id=args.user_id,
            org_id=args.org_id,
            session_name=args.name,
            template_id=args.template_id
        )
    )
