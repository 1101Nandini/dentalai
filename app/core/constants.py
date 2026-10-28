from enum import Enum


class DocumentStatus(str, Enum):
    SAVED = "saved"           # Uploaded to blob + DB, awaiting processing
    PARSING = "parsing"       # Worker claimed — parsing in progress
    PARSED = "parsed"         # Raw elements extracted, awaiting chunking
    CHUNKING = "chunking"     # Building RAG chunks
    CHUNKED = "chunked"       # Chunks saved, awaiting embedding
    EMBEDDING = "embedding"   # Embeddings being generated + indexed
    COMPLETED = "completed"   # Fully processed, searchable in knowledge base
    FAILED = "failed"         # Processing error (error_message on doc)
    SKIPPED = "skipped"       # No content to process (e.g., XLSX file itself)


class ChunkType(str, Enum):
    TEXT = "text"
    IMAGE = "image"          # Images / X-rays described by Azure Vision
    TABLE = "table"
    FIGURE = "figure"


class SubscriptionPlan(str, Enum):
    FREE_TRIAL = "free_trial"
    BASIC = "basic"
    PRO = "pro"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


# Usage limits per plan (number of AI queries)
PLAN_QUERY_LIMITS: dict[str, int] = {
    SubscriptionPlan.FREE_TRIAL: 500000,
    SubscriptionPlan.BASIC: 200,
    SubscriptionPlan.PRO: 1000,
}

# Collections
COLLECTION_USERS = "users"
COLLECTION_DOCUMENTS = "documents"
COLLECTION_CHUNKS = "chunks"
COLLECTION_CHATS = "chats"
COLLECTION_MESSAGES = "messages"
class UserRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    ORG_ADMIN = "org_admin"
    ADMIN = "admin"            # Legacy — kept for backward compat during migration
    DENTIST = "dentist"
    STAFF = "staff"
    FRONT_OFFICE = "front_office"
    STUDENT = "student"
    SOLO_DENTIST = "solo_dentist"



class DocumentStatus(str, Enum):
    SAVED = "saved"           # Uploaded to blob + DB, awaiting processing
    PARSING = "parsing"       # Worker claimed — parsing in progress
    PARSED = "parsed"         # Raw elements extracted, awaiting chunking
    CHUNKING = "chunking"     # Building RAG chunks
    CHUNKED = "chunked"       # Chunks saved, awaiting embedding
    EMBEDDING = "embedding"   # Embeddings being generated + indexed
    COMPLETED = "completed"   # Fully processed, searchable in knowledge base
    FAILED = "failed"         # Processing error (error_message on doc)
    SKIPPED = "skipped"       # No content to process (e.g., XLSX file itself)


class ChunkType(str, Enum):
    TEXT = "text"
    IMAGE = "image"          # Images / X-rays described by Azure Vision
    TABLE = "table"
    FIGURE = "figure"


class SubscriptionPlan(str, Enum):
    FREE_TRIAL = "free_trial"
    BASIC = "basic"
    PRO = "pro"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


# Usage limits per plan (number of AI queries)
PLAN_QUERY_LIMITS: dict[str, int] = {
    SubscriptionPlan.FREE_TRIAL: 500000,
    SubscriptionPlan.BASIC: 200,
    SubscriptionPlan.PRO: 1000,
}

# Collections
COLLECTION_USERS = "users"
COLLECTION_DOCUMENTS = "documents"
COLLECTION_CHUNKS = "chunks"
COLLECTION_CHATS = "chats"
COLLECTION_MESSAGES = "messages"
COLLECTION_CDT_CODES = "cdt_codes"
COLLECTION_CDT_DOCUMENTS = "cdt_documents"
COLLECTION_AUDIT_LOGS = "audit_logs"
COLLECTION_ORGANIZATIONS = "organizations"
COLLECTION_INVITATIONS = "invitations"
COLLECTION_ROLE_PERMISSIONS = "role_permissions"
COLLECTION_NOTE_TEMPLATES = "note_templates"
COLLECTION_ORG_TEMPLATE_VARIABLES = "org_template_variables"
COLLECTION_TEACHING_VIDEOS = "teaching_videos"
COLLECTION_CASE_STUDY = "case_study"
