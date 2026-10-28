# Infrastructure & Security Overview
**Organization:** Petal AI  
**System:** Petal AI Dental Assistant Backend  
**Version:** 2.0  
**Effective Date:** 2026-05-27  
**Last Updated:** 2026-10-12  
**Review Cycle:** Annually or after infrastructure changes  

---

## 1. Purpose

This document describes the infrastructure components, vendors, and security controls that Petal AI relies on. It provides auditors with a clear picture of where data lives, how it is protected, and which third parties are involved.

---

## 2. System Overview

Petal AI is a cloud-hosted FastAPI application serving as a dental AI assistant backend. It processes clinical documents, handles AI-powered chat via Retrieval-Augmented Generation (RAG), manages dental voice sessions with real-time transcription, provides subscription-based access control via Stripe, and offers customizable SOAP note templates, teaching videos, and clinical case studies.

---

## 3. Infrastructure Components

### 3.1 Application Server

| Property | Value |
|---|---|
| Framework | FastAPI (Python) |
| Server | Uvicorn (ASGI) |
| Process manager | PM2 (`ecosystem.config.js`) |
| Hosting | Self-hosted / VPS |
| Exposed URL | `https://app.smileguideai.com` |
| Internal port | 8000 |
| Deployment | Shell-based (`deploy.sh`: tar → SCP → PM2 restart) |

---

### 3.2 Database — MongoDB Atlas

| Property | Value |
|---|---|
| Provider | MongoDB Atlas (cloud-managed) |
| Database name | `dental_ai` |
| Encryption at rest | ✅ Enabled by default (Atlas standard) |
| Encryption in transit | ✅ TLS enforced by Atlas on all connections |
| Authentication | Connection string with credentials (stored in `MONGODB_URI` env var) |
| Backups | ✅ Automatic daily snapshots (Atlas default, 7-day retention) |
| Network access | Restricted to application server IP (configured in Atlas Network Access) |

**Collections and their purpose:**

| Collection | Data Stored |
|---|---|
| `users` | Account identity, hashed passwords, roles, login timestamps, org membership, active session IDs |
| `organizations` | Multi-tenant organization metadata, settings, logos |
| `documents` | Knowledge base document metadata, processing status, file hashes |
| `chunks` | Semantic text/image fragments + vector embeddings |
| `chats` | Conversation sessions per user |
| `messages` | Individual chat messages (user + AI), sources, image references |
| `otps` | Time-limited one-time passwords (auto-deleted via TTL) |
| `invitations` | Organization invitation tokens (auto-deleted via TTL) |
| `cdt_codes` | Dental procedure code reference data |
| `cdt_documents` | CDT codebook PDF metadata |
| `dental_voice_sessions` | Voice session transcripts, SOAP notes, and CDT coding results |
| `subscriptions` | User subscription plans, Stripe IDs, and status |
| `plans` | Subscription plan catalog (features, pricing, allowed roles) |
| `transaction_logs` | Payment transaction history |
| `audit_logs` | Immutable record of sensitive admin actions |
| `role_permissions` | DB-driven RBAC permission matrix per role |
| `note_templates` | Customizable SOAP note templates (global + org-specific) |
| `org_template_variables` | Per-organization template variable overrides |
| `teaching_videos` | Educational video/podcast metadata |
| `case_study` | Clinical case study content and embedding status |

---

### 3.3 File Storage — Azure Blob Storage

| Property | Value |
|---|---|
| Provider | Microsoft Azure |
| Container | `dental-documents` |
| Encryption at rest | ✅ Azure Storage Service Encryption (256-bit AES, enabled by default) |
| Encryption in transit | ✅ HTTPS enforced on all blob access |
| Access | Via Azure Storage Account Key (stored in env vars) |
| What's stored | Original uploaded PDFs, images, extracted figures, organization logos, audio recordings |

---

### 3.4 AI & Machine Learning Services

| Service | Provider | Purpose | Credentials |
|---|---|---|---|
| Azure OpenAI (text-embedding-3-large) | Microsoft Azure | Vector embeddings (3072D/1024D) | `AZURE_OPENAI_EMBEDDING_KEY` |
| Azure OpenAI (Whisper) | Microsoft Azure | Speech-to-text | `AZURE_OPENAI_API_KEY` |
| Azure AI Search | Microsoft Azure | Vector similarity search (4 indices) | `AZURE_SEARCH_KEY` |
| Azure Document Intelligence | Microsoft Azure | PDF/image text extraction (prebuilt-layout) | `AZURE_DOCUMENT_INTELLIGENCE_KEY` |
| Azure Computer Vision | Microsoft Azure | Image vectorization for similarity search | `AZURE_VISION_KEY` |
| Azure Speech | Microsoft Azure | Real-time speech synthesis | `AZURE_SPEECH_KEY` |
| Gemini (`gemini-3-flash-preview`) | Google Cloud / Vertex AI | Primary chat model + clinical enrichment + query analysis | `gcp-credentials.json` |
| Gemini (`gemini-3.1-flash-lite`) | Google Cloud / Vertex AI | Voice entity extraction + SOAP note generation | `gcp-credentials.json` |
| Gemini (`gemini-3.6-flash`) | Google Cloud / Vertex AI | Audio transcription (STT) | `gcp-credentials.json` |

All API keys are stored as environment variables. No keys are hardcoded in source code or committed to Git.

**Azure AI Search Indices:**

| Index Name | Purpose |
|---|---|
| `dental-text-index` | 3072D text/table chunk retrieval |
| `dental-image-index` | 1024D dual-vector image similarity + text-to-image search |
| `dental-cdt-index` | CDT code semantic matching |
| `dental-casestudy-index` | Case study similarity search |

---

### 3.5 Authentication & Identity

| Service | Provider | Purpose |
|---|---|---|
| Email/password auth | In-house (FastAPI + bcrypt + JWT) | Primary login |
| Google OAuth 2.0 | Google | Social sign-in |
| reCAPTCHA v2 | Google | Bot protection on login |
| OTP delivery | SMTP (Gmail) | Email verification & password reset |
| Notification emails | SMTP (Gmail) | Role changes, activation/deactivation, invitations |

---

### 3.6 Payments — Stripe

| Property | Value |
|---|---|
| Provider | Stripe |
| What Petal AI stores | Subscription plan type, status, and Stripe subscription/customer IDs |
| What Stripe stores | Payment card details (Petal AI never sees card data) |
| Webhook security | `STRIPE_WEBHOOK_SECRET` validates all incoming Stripe events |
| Checkout flow | Stripe-hosted Checkout Sessions |
| Payment verification | SSE-based polling (`/verify-payment/{session_id}`) |

---

## 4. Security Controls Summary

| Control | Status | Implementation |
|---|---|---|
| Passwords hashed | ✅ | bcrypt via `passlib` |
| JWT access tokens | ✅ | HS256, 60-min expiry |
| JWT refresh tokens | ✅ | 7-day expiry |
| Email verification (OTP) | ✅ | Required before first login |
| Role-based access control | ✅ | 7 roles (Super Admin, Org Admin, Dentist, Staff, Front Office, Student, Solo Dentist) |
| DB-driven permissions | ✅ | `role_permissions` collection, checked via `require_permission()` |
| Subscription gating | ✅ | Active subscription required for feature access |
| Rate limiting | ✅ | Per-route sliding window (Auth: 10/min, Chat: 50/min, etc.) |
| Malicious scanner blocking | ✅ | Regex pattern matching + permanent IP banning |
| Bot protection | ✅ | Google reCAPTCHA v2 |
| Student single-session | ✅ | JWT `session_id` vs. DB `active_session_id` enforcement |
| Audit logging | ✅ | Append-only `audit_logs` in MongoDB (13+ event types) |
| Server log rotation | ✅ | 10MB per file, 5 backups (50MB max) |
| Encryption at rest (DB) | ✅ | MongoDB Atlas default |
| Encryption at rest (files) | ✅ | Azure Blob SSE-256 default |
| Encryption in transit | ✅ | TLS enforced by Atlas + Azure |
| Credentials in env vars | ✅ | `.env` file, never in source code |
| HTTPS on production | ✅ | `https://app.smileguideai.com` |
| Secure WebSockets (WSS) | ✅ | Enforced on Dental Voice stream |
| CORS origin restriction | ✅ | Allowlist in `main.py` |
| Duplicate file detection | ✅ | SHA-256 hash deduplication |
| Zero Data Retention | ✅ | BAA signed; APIs do not train on patient data |
| Stripe webhook validation | ✅ | Signature verification on all incoming Stripe events |
| Organization-level deactivation | ✅ | Entire org can be deactivated, blocking all member access |

---

## 5. Network Architecture

```
Internet
  │
  ├─► REST API (HTTPS / TLS)
  └─► Real-Time Dental Voice (WSS / TLS)
        │
        ▼
https://app.smileguideai.com:8000
  │
  ▼
Application Server (FastAPI + Uvicorn + PM2)
  │
  ├──► MongoDB Atlas          (TLS, credentials)
  ├──► Azure Blob Storage     (HTTPS, account key)
  ├──► Azure AI Search        (HTTPS, API key, 4 indices)
  ├──► Azure OpenAI           (HTTPS, API key, Zero Data Retention BAA)
  ├──► Azure Document Intel.  (HTTPS, API key)
  ├──► Azure Computer Vision  (HTTPS, API key)
  ├──► Azure Speech           (HTTPS, API key)
  ├──► Google Vertex AI       (HTTPS, service account, 3 Gemini models)
  ├──► Stripe                 (HTTPS, secret key + webhook secret)
  ├──► Google OAuth 2.0       (HTTPS, client credentials)
  ├──► Google reCAPTCHA       (HTTPS, secret key)
  └──► Gmail SMTP             (TLS, app password)
```

---

## 6. Vendor Security Posture

| Vendor | SOC 2 Status |
|---|---|
| Microsoft Azure | SOC 2 Type II certified |
| MongoDB Atlas | SOC 2 Type II certified |
| Google Cloud | SOC 2 Type II certified |
| Stripe | SOC 2 Type II certified |

Petal AI inherits significant security controls from these certified vendors for the infrastructure layer.

---

## 7. Known Gaps & Remediation Plan

| Gap | Remediation | Target |
|---|---|---|
| No formal MFA for admin login | Implement TOTP-based 2FA for admin account | Q3 2026 |
| `SECRET_KEY` default in config | Enforce non-default key validation at startup | Q2 2026 |
| No formal penetration test | Engage security firm for pentest | Before Type I audit |
| Auditor not yet engaged | Select SOC 2 auditor (Vanta/Drata/CPA firm) | Q3 2026 |

---

**Document Owner:** Petal AI Admin  
**Last Reviewed:** 2026-10-12
