# Data Retention & Deletion Policy
**Organization:** Petal AI  
**System:** Petal AI Dental Assistant Backend  
**Version:** 2.0  
**Effective Date:** 2026-05-27  
**Last Updated:** 2026-10-12  
**Review Cycle:** Annually  

---

## 1. Purpose

This policy defines how long Petal AI retains different types of data and when data must be permanently deleted. It ensures data is not kept longer than necessary and that deletion requests are handled appropriately.

---

## 2. Data Categories & Retention Periods

### 2.1 User Accounts (`users` collection)

| Scenario | Retention |
|---|---|
| Active account | Retained indefinitely while account is active |
| Deactivated account (by admin) | Retained for **90 days** post-deactivation, then permanently deleted |
| User requests deletion | Deleted within **30 days** of request |

**What is stored:** email, full name, hashed password (bcrypt), role, subscription plan, login timestamps, `has_agreed` (disclaimer), `active_session_id` (students), `account_type`, `organization_id`  
**What is NOT stored:** plain-text passwords, payment card details

---

### 2.2 OTPs (`otps` collection)

| Type | Retention |
|---|---|
| Unused OTP | Auto-deleted after **10 minutes** (MongoDB TTL index on `expires_at`) |
| Used OTP (after verification) | Marked `used: true` and auto-deleted with TTL |

**Implementation:** MongoDB TTL index (`expireAfterSeconds: 0` on `expires_at` field) handles deletion automatically — no manual process required.

---

### 2.3 Knowledge Base Documents (`documents`, `chunks` collections + Azure Blob)

| Scenario | Retention |
|---|---|
| Active document in knowledge base | Retained until Admin explicitly deletes it |
| Deleted document | Removed immediately from MongoDB + Azure Blob Storage on delete request |

**What is stored:** Original filename, file hash (SHA-256), file type, blob name, processing status, extracted text chunks, vector embeddings  
**Storage location:** Azure Blob Storage (file) + MongoDB Atlas (metadata + chunks) + Azure AI Search (indexed vectors)

---

### 2.4 Chat History (`chats`, `messages` collections)

| Scenario | Retention |
|---|---|
| Active user conversations | Retained while user account is active |
| User account deleted | Chat history deleted within **30 days** of account deletion |

**What is stored:** User messages, AI responses, cited sources, image references  
**Note:** Chat data is not shared across users. Each user's history is isolated by `user_id`.

---

### 2.5 Dental Voice Sessions (`dental_voice_sessions` collection)

| Scenario | Retention |
|---|---|
| Active session | Subject to the Organization's `auto_delete_sessions` policy |
| Auto-delete policy enabled | Sessions older than **30 days** (or the org's configured threshold) are permanently deleted by the SOAP retention worker |
| Auto-delete policy disabled| Retained indefinitely until manual deletion |
| Deleted session | Removed immediately from MongoDB + Azure Blob Storage (audio files) on delete request |

**What is stored:** Audio PCM blobs (Azure Storage), transcripts, extracted SOAP notes, CDT codes, session type, verification status.  
**Implementation:** `soap_retention_worker` runs as a background asyncio task, querying all orgs with `auto_delete_sessions=true` and purging expired records.

---

### 2.6 Invitations (`invitations` collection)

| Scenario | Retention |
|---|---|
| Pending invitation | Auto-deleted after expiry (MongoDB TTL index on `expires_at`) |
| Used invitation | Marked as used, auto-deleted with TTL |

**Implementation:** MongoDB TTL index handles automatic cleanup.

---

### 2.7 Audit Logs (`audit_logs` collection)

| Scenario | Retention |
|---|---|
| All audit events | Retained for **2 years** minimum |
| After 2 years | May be archived or deleted at Admin discretion |

**Rationale:** Audit logs exist for compliance and incident investigation. They must be retained long enough to cover any potential investigation window.

**What is stored:** actor_id, action type, resource, resource_id, metadata, timestamp, organization_id  
**What is NOT stored:** full request/response bodies, passwords, tokens

---

### 2.8 Server Logs (`app_server.log`)

| Scenario | Retention |
|---|---|
| Active log file | Max 10MB per file |
| Rotated files | Max 5 backup files (`.1` to `.5`) |
| **Total on disk** | ~50MB maximum, auto-managed by rotating file handler |
| Oldest logs | Auto-deleted when 6th rotation occurs |

**Effective retention window:** Approximately the last 30–90 days of activity depending on log volume.

---

### 2.9 Subscription & Payment Records (`subscriptions`, `transaction_logs`, `plans` collections)

| Scenario | Retention |
|---|---|
| Active subscription | Retained while account is active |
| Cancelled/expired subscription | Retained for **7 years** (financial record requirement) |
| Transaction logs | Retained for **7 years** |

**Note:** Petal AI does not store credit card numbers. All payment data is handled by Stripe. Only subscription plan type, status, and Stripe subscription IDs are stored in MongoDB.

---

### 2.10 Templates & Educational Content

| Data | Collection | Retention |
|---|---|---|
| SOAP Note Templates | `note_templates` | Retained until admin deletes; soft-delete via `is_active` flag |
| Template Variables | `org_template_variables` | Retained while org is active |
| Teaching Videos | `teaching_videos` | Retained until admin deletes; soft-delete via `is_active` flag |
| Case Studies | `case_study` | Retained until admin deletes |
| CDT Codes | `cdt_codes` | Retained until associated CDT document is deleted |
| CDT Documents | `cdt_documents` | Retained until admin deletes (cascades to codes) |

---

### 2.11 Role Permissions (`role_permissions` collection)

| Scenario | Retention |
|---|---|
| Active permission records | Retained indefinitely (seeded on startup, editable at runtime) |

**Note:** Permission records are the source of truth for RBAC after initial seeding and should not be deleted.

---

## 3. Deletion Procedures

### 3.1 User-Requested Deletion
1. User contacts Admin requesting account deletion
2. Admin verifies identity
3. Admin deletes account via `DELETE /api/v1/users/{id}`
4. Associated chat history and messages are deleted within 30 days
5. Deletion is recorded in `audit_logs`
6. Subscription records are retained per Section 2.9

### 3.2 Document Deletion
- Admin calls `DELETE /api/v1/documents/{id}`
- MongoDB document record, all associated chunks, and Azure Blob file are deleted immediately
- Indexed vectors in Azure AI Search are also removed
- Deletion is recorded in `audit_logs` with `action: "DOCUMENT_DELETE"`

### 3.3 Dental Voice Deletion
- Org Admin or Dentist calls `DELETE /api/v1/dental-voice/sessions/{id}` (or bulk delete)
- MongoDB record and the raw Azure Blob audio recording are permanently destroyed
- Deletion is recorded in `audit_logs` with `action: "REPORT_DELETE"`

### 3.4 Organization Deletion
- Super Admin calls `DELETE /api/v1/organizations/{org_id}`
- All associated data is permanently deleted: members, sessions, invitations, templates, audit logs
- This is a cascading, irreversible operation

### 3.5 CDT Document Deletion
- Admin calls `DELETE /api/v1/dental-voice/cdt-documents/{id}`
- CDT document metadata and all associated CDT codes are permanently deleted
- Deletion is recorded in `audit_logs` with `document_type: "CDT_CODEBOOK"`

### 3.6 Automated Deletion (TTL & Background Workers)
- **OTPs**: MongoDB TTL indexes handle automatic OTP deletion.
- **Invitations**: MongoDB TTL indexes handle automatic invitation cleanup.
- **Voice Sessions**: The `soap_retention_worker` background task runs daily to enforce each organization's `auto_delete_sessions` policy (typically set to 30 days).

---

## 4. Data That Is Never Stored

Petal AI explicitly does **not** store:
- Plain-text passwords (only bcrypt hashes)
- Credit card or payment details (handled entirely by Stripe)
- Full JWT tokens (tokens are stateless and not persisted)
- Patient names or health records directly (only dental documents uploaded by staff)
- reCAPTCHA tokens (validated server-side and discarded)

---

## 5. Backup & Recovery

| Service | Backup Owner | Backup Frequency |
|---|---|---|
| MongoDB Atlas | MongoDB Atlas (automatic) | Daily snapshots, 7-day retention (Atlas default) |
| Azure Blob Storage | Azure (geo-redundant storage) | Continuous replication |
| Application code | GitLab repository | Version controlled |

---

**Policy Owner:** Petal AI Admin  
**Last Reviewed:** 2026-10-12
