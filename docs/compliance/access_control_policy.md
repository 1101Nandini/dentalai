# Access Control Policy
**Organization:** Petal AI  
**System:** Petal AI Dental Assistant Backend  
**Version:** 2.0  
**Effective Date:** 2026-05-27  
**Last Updated:** 2026-10-12  
**Review Cycle:** Annually  

---

## 1. Purpose

This policy defines how access to Petal AI systems, data, and infrastructure is granted, managed, and revoked. It ensures that only authorized individuals can access clinical data, documents, and administrative functions.

---

## 2. Scope

Applies to all users of the Petal AI platform including:
- Internal team members (Super Admin, Org Admin, Dentists, Staff, Front Office)
- Solo Dentists (individual practitioners without an organization)
- Students and trainees
- Third-party services (Azure, MongoDB Atlas, Google Cloud, Stripe)

---

## 3. User Roles & Permissions

Petal AI enforces **Multi-Tenant Role-Based Access Control (RBAC)** using a **dual-layer** system:
1.  **Code-level guards** (`dependencies.py`): FastAPI dependencies like `get_super_admin`, `get_org_admin`, `get_admin_or_dentist`, etc.
2.  **DB-driven permissions** (`role_permissions` collection): Fine-grained permission keys checked via `require_permission("key")` dependency. Seeded from `PermissionService.seed_defaults()` on startup; the DB becomes the source of truth thereafter.

### 3.1 Role Definitions

Roles are defined in `app/core/constants.py` as the `UserRole` enum:

| Role | Description | Key Permissions |
|---|---|---|
| **SUPER_ADMIN** | Global System Administrator | Full access across all organizations. Can create/delete orgs, manage billing, alter system-wide settings, manage knowledge base, upload CDT codebooks, manage videos, manage case studies. |
| **ORG_ADMIN** | Organization Administrator | Manages a specific clinic. Can invite staff, configure org settings (`auto_delete_sessions`), view org analytics, bulk delete sessions, manage members, customize SOAP note templates, access financial bridge. |
| **DENTIST** | Licensed Provider | Can stream Dental Voice sessions, generate SOAP notes, delete their own sessions, use AI chat, view CDT codes, view templates. |
| **STAFF** | Clinic Support Staff | Can view and edit clinical reports within their org, stream voice sessions, view CDT codes. **Cannot delete sessions**. |
| **FRONT_OFFICE** | Front Desk Personnel | Can view and edit sessions within their org, stream voice sessions, view CDT codes. **Cannot use AI chat or delete sessions**. |
| **STUDENT** | Trainee / Student | Access to AI chat and teaching videos only. **Cannot access voice sessions, documents, or clinical data**. Enforces single-device login. |
| **SOLO_DENTIST** | Individual Practitioner | Same as Dentist but operates independently without an organization. Can manage their own templates and variables. |
| **ADMIN** *(Legacy)* | Kept for backward compatibility during migration. Maps to `org_admin_or_super` in permission checks. |

### 3.2 Permission Matrix (Key Permissions)

| Permission Key | Super Admin | Org Admin | Dentist | Staff | Front Office | Student | Solo Dentist |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `sessions:list` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `sessions:view` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `sessions:edit` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `sessions:delete` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ |
| `sessions:bulk_delete` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `sessions:org_access` | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ |
| `documents:upload` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `documents:list` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `documents:delete` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `chat:use` | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| `analytics:dashboard` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `org:manage_members` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `org:invite` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `voice:stream` | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `templates:manage` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ |
| `videos:list` | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |
| `videos:manage` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `case_study:manage` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `financial_bridge:access` | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |

### 3.3 Restricted Actions (Super Admin Only)
- Access Global System Dashboard (`GET /api/v1/analytics/dashboard` without org filter)
- Create/Delete organizations (`POST /api/v1/organizations`, `DELETE /api/v1/organizations/{id}`)
- Upload knowledge base documents (`POST /api/v1/documents/upload`)
- Delete knowledge base documents (`DELETE /api/v1/documents/{id}`)
- Upload CDT codebook PDFs
- Manage teaching videos (CRUD)
- Manage case studies (CRUD + trigger embedding)
- Delete user accounts (`DELETE /api/v1/users/{id}`)
- View system-wide audit logs

### 3.4 Restricted Actions (Org Admin Only)
- Invite new staff to the organization (`GET /api/v1/org/invite-link`)
- Change member roles (`PUT /api/v1/org/members/{id}/role`)
- Activate/deactivate members (`PUT /api/v1/org/members/{id}/status`)
- Bulk delete clinical sessions within the org (`DELETE /api/v1/dental-voice/sessions`)
- Manage SOAP note templates and template variables for their org

### 3.5 Student Session Enforcement
Student accounts enforce **single-device login**:
- On login, a unique `session_id` is embedded in the JWT and stored as `active_session_id` in the user document.
- On every authenticated request, the middleware compares the JWT's `session_id` against the DB's `active_session_id`.
- If they don't match (another device logged in), the request is rejected with `SESSION_REPLACED`.

---

## 4. Account Lifecycle

### 4.1 Account Creation
- New accounts are created via `/api/v1/auth/register` or Google OAuth (`/api/v1/auth/google`)
- Email verification via OTP is **required** before login is permitted (`is_verified = true`)
- For org-based registration, users register via an invitation link that auto-assigns them to the organization with the configured role
- A disclaimer acceptance step (`has_agreed`) is tracked per user

### 4.2 Role Assignment & Promotion
- Roles are explicitly assigned when:
  - The Org Admin generates an invite link with a specific role
  - The Org Admin changes a member's role via `PUT /api/v1/org/members/{id}/role`
- Every role change is recorded in the `audit_logs` MongoDB collection with:
  - `actor_id` (admin who made the change)
  - `organization_id`
  - `action: "ROLE_CHANGE"`
  - `new_role` (the role assigned)
  - `timestamp`
- **Notification emails** are automatically sent on role changes

### 4.3 Account Deactivation
- Accounts are deactivated (`is_active = false`) by the Org Admin via `PUT /api/v1/org/members/{id}/status`
- Deactivated users are immediately blocked from all API access (checked in `get_current_user`)
- Deactivation events are recorded in `audit_logs` with `action: "USER_DEACTIVATE"`
- **Notification emails** are sent on both activation and deactivation
- Organization-level deactivation is also supported (`is_active` on the org document)

### 4.4 Account Offboarding
- When a team member leaves, the Admin must deactivate their account immediately
- Account deactivation target: **within 24 hours** of departure

---

## 5. Authentication Controls

| Control | Implementation |
|---|---|
| Password hashing | `bcrypt` via `passlib` |
| Session tokens | Short-lived JWT (60 min access token, 7-day refresh token) |
| Token algorithm | HS256 |
| Email verification | OTP-based, expires in 10 minutes (MongoDB TTL index) |
| Bot protection | Google reCAPTCHA v2 on login |
| Social login | Google OAuth 2.0 (ID token verified server-side) |
| Rate limiting | Per-route sliding window — Auth: 10 req/min, Chat: 50 req/min, Documents: 30 req/min, etc. |
| Single-device enforcement | Students only — JWT `session_id` vs. DB `active_session_id` |
| Org activity check | User's organization `is_active` flag verified on every request |

---

## 6. Subscription-Based Access Control

Beyond RBAC, the `PermissionService` enforces **subscription gating**:
- Each user (or organization) must have an active subscription in the `subscriptions` collection.
- Subscription plans are defined in the `plans` collection with `allowed_roles` and feature flags.
- When a permission check runs, the service validates both role permissions AND active subscription status.
- Plans support: `free_trial`, `basic`, `pro` tiers with configurable query limits.

---

## 7. Third-Party Access

| Service | Access Type | Purpose |
|---|---|---|
| MongoDB Atlas | Service account (connection string) | Primary database |
| Azure (Storage, AI Search, OpenAI, Speech, Vision, Document Intelligence) | API key per service | Document processing & AI |
| Google Cloud / Vertex AI | Service account JSON (`gcp-credentials.json`) | Gemini AI models |
| Stripe | Secret key + webhook secret | Subscription billing |
| Google OAuth 2.0 | Client ID + Client Secret | Social sign-in |
| Google reCAPTCHA v2 | Secret key | Bot protection |
| SMTP (Gmail) | App password | Email delivery (OTP, notifications) |

All third-party credentials are stored as **environment variables** (`.env` file), never hardcoded in source code.

---

## 8. Audit Logging

Every sensitive action is recorded in the `audit_logs` MongoDB collection:

| Event | Trigger |
|---|---|
| `LOGIN` | Successful email/password login |
| `ORG_CREATE` | A new multi-tenant organization is provisioned |
| `ORG_UPDATE` | Org Admin modifies settings (e.g., `auto_delete_sessions`) |
| `INVITATION_SEND` | Org Admin sends an invitation |
| `ROLE_CHANGE` | Org Admin changes a user's role |
| `USER_DEACTIVATE` | Org Admin deactivates a user account |
| `USER_DELETE` | Super Admin deletes a user account |
| `DOCUMENT_DELETE` | Admin or Dentist deletes a document |
| `REPORT_DELETE` | Admin or Dentist deletes a Dental Voice session |
| `SUBSCRIPTION_CHECKOUT_INITIATED` | User starts a Stripe checkout session |
| `SUBSCRIPTION_CANCEL_REQUESTED` | User requests subscription cancellation |
| `SUBSCRIPTION_RESUME_REQUESTED` | User resumes a cancelled subscription |
| `SUBSCRIPTION_TRIAL_SKIPPED` | User skips trial phase |

Audit logs are append-only inserts and are **never modified or deleted**. Every log is scoped to its `organization_id` to prevent cross-tenant data leaks.

---

## 9. Policy Violations

Any access control violation (unauthorized access attempt, privilege abuse) must be reported to the Admin immediately. Violations may result in account suspension and further review.

---

## 10. Review

This policy is reviewed annually or after any significant system change or security incident.

**Policy Owner:** Petal AI Admin  
**Last Reviewed:** 2026-10-12
