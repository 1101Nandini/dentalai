import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from bson import ObjectId
from fastapi import HTTPException
import httpx

from app.core.database import get_database
from app.core.constants import COLLECTION_NOTE_TEMPLATES, COLLECTION_ORG_TEMPLATE_VARIABLES
from app.templates.default_templates import DEFAULT_TEMPLATES
from app.templates.soap_template import TEMPLATE_VALIDATION_PROMPT
from app.config import settings
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage
import re

logger = logging.getLogger(__name__)


class TemplateService:

    # ── Seeding ─────────────────────────────────────────────────────────────

    @staticmethod
    async def seed_default_templates() -> dict:
        """Seed the 12 default global templates."""
        db = await get_database()
        now = datetime.now(timezone.utc)
        seeded = 0

        for tpl in DEFAULT_TEMPLATES:
            # We use procedure_name and is_default to find existing ones
            query = {"procedure_name": tpl["procedure_name"], "is_default": True, "organization_id": None}
            existing = await db[COLLECTION_NOTE_TEMPLATES].find_one(query)
            if not existing:
                new_tpl = {
                    "organization_id": None,
                    "source_template_id": None,
                    "is_custom": False,
                    "is_default": True,
                    "is_active": True,
                    "procedure_name": tpl["procedure_name"],
                    "cdt_refs": tpl["cdt_refs"],
                    "trigger_keywords": tpl["trigger_keywords"],
                    "video_url": tpl.get("video_url"),
                    "fields": tpl.get("fields", []),
                    "body_template": tpl["body_template"],
                    "created_at": now,
                    "updated_at": now
                }
                await db[COLLECTION_NOTE_TEMPLATES].insert_one(new_tpl)
                seeded += 1

        return {
            "total_defaults": len(DEFAULT_TEMPLATES),
            "newly_seeded": seeded,
            "already_existed": len(DEFAULT_TEMPLATES) - seeded
        }

    # ── Org Variables ───────────────────────────────────────────────────────

    @staticmethod
    async def get_org_variables_dict(org_id: str) -> dict:
        """Fetch all variables for an org and return as a flat dictionary (used internally by Ghostwriter)."""
        db = await get_database()
        cursor = db[COLLECTION_ORG_TEMPLATE_VARIABLES].find({"organization_id": org_id})
        variables = {}
        async for doc in cursor:
            if "name" in doc and "value" in doc:
                variables[doc["name"]] = doc["value"]
        return variables

    @staticmethod
    async def create_org_variable(org_id: str, name: str, value: str, updated_by: str) -> dict:
        db = await get_database()
        now = datetime.now(timezone.utc)
        
        existing = await db[COLLECTION_ORG_TEMPLATE_VARIABLES].find_one({"organization_id": org_id, "name": name})
        if existing:
            raise HTTPException(status_code=400, detail=f"Variable '{name}' already exists")
            
        doc = {
            "organization_id": org_id,
            "name": name,
            "value": value,
            "updated_at": now,
            "updated_by": updated_by
        }
        result = await db[COLLECTION_ORG_TEMPLATE_VARIABLES].insert_one(doc)
        doc["id"] = str(result.inserted_id)
        del doc["_id"]
        doc["updated_at"] = doc["updated_at"].isoformat()
        return doc

    @staticmethod
    async def list_org_variables(org_id: str, skip: int = 0, limit: int = 10, search_name: Optional[str] = None) -> dict:
        db = await get_database()
        query = {"organization_id": org_id}
        if search_name:
            query["name"] = {"$regex": search_name, "$options": "i"}
            
        total = await db[COLLECTION_ORG_TEMPLATE_VARIABLES].count_documents(query)
        cursor = db[COLLECTION_ORG_TEMPLATE_VARIABLES].find(query).sort("name", 1).skip(skip).limit(limit)
        
        items = []
        async for doc in cursor:
            doc["id"] = str(doc["_id"])
            del doc["_id"]
            if "updated_at" in doc and hasattr(doc["updated_at"], "isoformat"):
                doc["updated_at"] = doc["updated_at"].isoformat()
            items.append(doc)
            
        return {"items": items, "total": total, "skip": skip, "limit": limit}

    @staticmethod
    async def get_org_variable(org_id: str, var_id: str) -> dict:
        db = await get_database()
        try:
            doc = await db[COLLECTION_ORG_TEMPLATE_VARIABLES].find_one({"_id": ObjectId(var_id), "organization_id": org_id})
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid variable ID format")
            
        if not doc:
            raise HTTPException(status_code=404, detail="Variable not found")
            
        doc["id"] = str(doc["_id"])
        del doc["_id"]
        if "updated_at" in doc and hasattr(doc["updated_at"], "isoformat"):
            doc["updated_at"] = doc["updated_at"].isoformat()
        return doc

    @staticmethod
    async def update_org_variable(org_id: str, var_id: str, value: str, updated_by: str) -> dict:
        db = await get_database()
        try:
            obj_id = ObjectId(var_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid variable ID format")
            
        now = datetime.now(timezone.utc)
        result = await db[COLLECTION_ORG_TEMPLATE_VARIABLES].update_one(
            {"_id": obj_id, "organization_id": org_id},
            {"$set": {"value": value, "updated_at": now, "updated_by": updated_by}}
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Variable not found")
            
        return await TemplateService.get_org_variable(org_id, var_id)

    @staticmethod
    async def delete_org_variable(org_id: str, var_id: str) -> dict:
        db = await get_database()
        try:
            obj_id = ObjectId(var_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid variable ID format")
            
        doc = await TemplateService.get_org_variable(org_id, var_id)
        await db[COLLECTION_ORG_TEMPLATE_VARIABLES].delete_one({"_id": obj_id, "organization_id": org_id})
        return doc


    # ── CRUD ─────────────────────────────────────────────────────────────

    @staticmethod
    async def list_templates(org_id: Optional[str] = None, active_only: bool = True) -> List[dict]:
        """List global defaults AND org-specific templates."""
        db = await get_database()
        query = {}
        if active_only:
            query["is_active"] = True

        if org_id:
            query["$or"] = [
                {"organization_id": None},
                {"organization_id": org_id}
            ]
        else:
            query["organization_id"] = None

        cursor = db[COLLECTION_NOTE_TEMPLATES].find(query).sort("procedure_name", 1)
        templates = []
        async for t in cursor:
            t["id"] = str(t.pop("_id"))
            if t.get("source_template_id"):
                t["source_template_id"] = str(t["source_template_id"])
            for date_field in ["created_at", "updated_at"]:
                if date_field in t and t[date_field]:
                    t[date_field] = t[date_field].isoformat()
            templates.append(t)
        return templates

    @staticmethod
    async def get_template(template_id: str, org_id: Optional[str] = None, is_super_admin: bool = False) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(template_id):
            raise HTTPException(status_code=400, detail="Invalid template ID")
            
        t = await db[COLLECTION_NOTE_TEMPLATES].find_one({"_id": ObjectId(template_id)})
        if not t:
            raise HTTPException(status_code=404, detail="Template not found")
            
        # Ensure org can only access globals or their own
        if not is_super_admin and t.get("organization_id") and t["organization_id"] != org_id:
            raise HTTPException(status_code=403, detail="Access denied to this template")

        t["id"] = str(t.pop("_id"))
        if t.get("source_template_id"):
            t["source_template_id"] = str(t["source_template_id"])
        for date_field in ["created_at", "updated_at"]:
            if date_field in t and t[date_field]:
                t[date_field] = t[date_field].isoformat()
        return t

    @staticmethod
    async def create_template(org_id: Optional[str], data: dict, created_by: str, is_super_admin: bool = False) -> dict:
        # Validate through AI first
        validation = await TemplateService.validate_template(data)
        if not validation.get("is_valid", False):
            raise HTTPException(
                status_code=400, 
                detail=f"Template failed AI compliance validation: {validation.get('reason', 'Unknown error')}"
            )

        db = await get_database()
        now = datetime.now(timezone.utc)
        
        is_default = is_super_admin or (org_id is None)
        
        new_tpl = {
            "organization_id": None if is_default else org_id,
            "source_template_id": None,
            "is_custom": not is_default,
            "is_default": is_default,
            "is_active": True,
            "procedure_name": data["procedure_name"],
            "cdt_refs": data.get("cdt_refs", []),
            "video_url": data.get("video_url"),
            "body_template": data["body_template"],
            "created_by": created_by,
            "created_at": now,
            "updated_at": now
        }
        
        result = await db[COLLECTION_NOTE_TEMPLATES].insert_one(new_tpl)
        new_tpl["id"] = str(new_tpl.pop("_id"))
        new_tpl["created_at"] = new_tpl["created_at"].isoformat()
        new_tpl["updated_at"] = new_tpl["updated_at"].isoformat()
        return new_tpl

    @staticmethod
    async def update_template(template_id: str, org_id: Optional[str], data: dict, updated_by: str, is_super_admin: bool = False) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(template_id):
            raise HTTPException(status_code=400, detail="Invalid template ID")
            
        t = await db[COLLECTION_NOTE_TEMPLATES].find_one({"_id": ObjectId(template_id)})
        if not t:
            raise HTTPException(status_code=404, detail="Template not found")
            
        if not is_super_admin and t.get("organization_id") != org_id:
            raise HTTPException(status_code=403, detail="Cannot modify global defaults or other org templates")

        # Validate through AI if body or fields changed
        if "body_template" in data or "fields" in data:
            check_data = {
                "body_template": data.get("body_template", t.get("body_template", "")),
                "fields": data.get("fields", t.get("fields", []))
            }
            validation = await TemplateService.validate_template(check_data)
            if not validation.get("is_valid", False):
                raise HTTPException(
                    status_code=400, 
                    detail=f"Template update failed AI compliance validation: {validation.get('reason')}"
                )

        now = datetime.now(timezone.utc)
        set_fields = {k: v for k, v in data.items() if v is not None}
        set_fields["updated_at"] = now
        set_fields["updated_by"] = updated_by

        result = await db[COLLECTION_NOTE_TEMPLATES].find_one_and_update(
            {"_id": ObjectId(template_id)},
            {"$set": set_fields},
            return_document=True
        )
        
        result["id"] = str(result.pop("_id"))
        if result.get("source_template_id"):
            result["source_template_id"] = str(result["source_template_id"])
        for date_field in ["created_at", "updated_at"]:
            if date_field in result and result[date_field]:
                result[date_field] = result[date_field].isoformat()
        return result

    @staticmethod
    async def delete_template(template_id: str, org_id: Optional[str], is_super_admin: bool = False) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(template_id):
            raise HTTPException(status_code=400, detail="Invalid template ID")
            
        t = await db[COLLECTION_NOTE_TEMPLATES].find_one({"_id": ObjectId(template_id)})
        if not t:
            raise HTTPException(status_code=404, detail="Template not found")
            
        if not is_super_admin and t.get("organization_id") != org_id:
            raise HTTPException(status_code=403, detail="Cannot delete global defaults or other org templates")

        await db[COLLECTION_NOTE_TEMPLATES].delete_one({"_id": ObjectId(template_id)})
        
        t["id"] = str(t.pop("_id"))
        return t

    # ── AI Validation ───────────────────────────────────────────────────────

    @staticmethod
    async def validate_template(template_data: dict) -> dict:
        """Call LLM to validate the template content for compliance."""
        try:
            llm = ChatGoogleGenerativeAI(
                model=settings.GEMINI_CHAT_MODEL,
                project=settings.GOOGLE_CLOUD_PROJECT,
                location=settings.GOOGLE_CLOUD_LOCATION,
                credentials=settings.get_vertex_credentials(),
                vertexai=True,
                temperature=0.0,
                top_p=0.9,
                thinking_budget=0,
            )

            user_msg = f"Please validate this template:\n{json.dumps(template_data, indent=2)}"
            
            response = await llm.ainvoke([
                SystemMessage(content=TEMPLATE_VALIDATION_PROMPT),
                HumanMessage(content=user_msg)
            ])
            
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )

            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)

            result = json.loads(content)
            return result
        except Exception as e:
            logger.error(f"[TEMPLATE_VAL] Failed to parse LLM response: {e}")
            return {"is_valid": result.get("is_valid", False), "reason": result.get("reason", "Unknown")}

    @staticmethod
    async def enhance_template(raw_text: str, procedure_name: str = None) -> dict:
        """Call LLM to enhance rough text into a formatted template schema."""
        from app.templates.soap_template import TEMPLATE_ENHANCEMENT_PROMPT
        import re
        try:
            llm = ChatGoogleGenerativeAI(
                model=settings.GEMINI_CHAT_MODEL,
                project=settings.GOOGLE_CLOUD_PROJECT,
                location=settings.GOOGLE_CLOUD_LOCATION,
                credentials=settings.get_vertex_credentials(),
                vertexai=True,
                temperature=0.2,
            )

            context = f"ROUGH TEXT:\n{raw_text}\n"
            if procedure_name:
                context += f"SUGGESTED NAME: {procedure_name}\n"

            # Fetch 2 default templates to use as style references
            db = await get_database()
            default_templates = await db[COLLECTION_NOTE_TEMPLATES].find({"is_default": True}).limit(2).to_list(length=2)
            if default_templates:
                context += "\n--- STYLE REFERENCE TEMPLATES ---\n"
                for t in default_templates:
                    context += f"Template Name: {t.get('procedure_name')}\nTemplate Body:\n{t.get('body_template')}\n\n"
                context += "---------------------------------\n"
                
            # Fetch relevant CDT codes from our Vector DB
            try:
                from app.services.cdt_service import CDTService
                suggested_codes = await CDTService._search_cdt_index(raw_text, top_k=4)
                if suggested_codes:
                    context += "\n--- CDT CODE HINTS FROM DATABASE ---\n"
                    context += "Here are some CDT codes from our database that semantically match the text. Choose the best fit:\n"
                    for code in suggested_codes:
                        context += f"- {code.get('code')}: {code.get('description', '')}\n"
                    context += "------------------------------------\n"
            except Exception as e:
                logger.warning(f"[TEMPLATE_SERVICE] Could not fetch CDT hints: {e}")

            response = await llm.ainvoke([
                SystemMessage(content=TEMPLATE_ENHANCEMENT_PROMPT),
                HumanMessage(content=context)
            ])

            content = response.content
            if isinstance(content, list):
                content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)

            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)

            return json.loads(content)

        except Exception as e:
            logger.error(f"[TEMPLATE_SERVICE] Enhancement failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to enhance template")
