# Incident Response Plan
**Organization:** Petal AI  
**System:** Petal AI Dental Assistant Backend  
**Version:** 2.0  
**Effective Date:** 2026-05-27  
**Last Updated:** 2026-10-12  
**Review Cycle:** Annually  

---

## 1. Purpose

This plan defines how Petal AI detects, responds to, and recovers from security incidents. The goal is to minimize damage, restore service, and notify affected parties within legally required timeframes.

---

## 2. What Counts as a Security Incident

| Severity | Examples |
|---|---|
| **Critical** | Database breach, unauthorized admin access, patient data exposed, API keys leaked, `gcp-credentials.json` leaked |
| **High** | User account compromised, ransomware, mass unauthorized data access, Stripe webhook secret compromised |
| **Medium** | Failed brute-force attacks above threshold, suspicious role changes, token forgery attempt, abnormal subscription activity |
| **Low** | Single failed login attempt, rate limit triggered, minor config misconfiguration, scanner probe blocked |

---

## 3. Incident Response Team

Since Petal AI currently operates with a small team:

| Role | Responsibility |
|---|---|
| **Admin** | Primary responder — detects, contains, and coordinates response |
| **Developer** | Technical investigation and fix deployment |

---

## 4. Response Steps

### Step 1: Detect
**Sources to monitor:**
- `audit_logs` MongoDB collection — unusual `ROLE_CHANGE`, `USER_DELETE`, `DOCUMENT_DELETE`, `SUBSCRIPTION_*` patterns
- `app_server.log` files — look for repeated 401/403 errors, JWTError patterns, rate limit violations
- Rate limit middleware — check `PERMANENTLY_BANNED_IPS` set for automatically banned scanners
- MongoDB Atlas alerts (if configured)
- Azure Security Center alerts (if configured)
- Stripe Dashboard — unusual payment activity or webhook failures

**Detection query example (MongoDB):**
```js
// Find all logins in the last 24 hours
db.audit_logs.find({
  action: "LOGIN",
  timestamp: { $gte: new Date(Date.now() - 86400000) }
})

// Find all role changes ever
db.audit_logs.find({ action: "ROLE_CHANGE" })

// Find all subscription-related events in the last week
db.audit_logs.find({
  action: { $regex: /^SUBSCRIPTION_/ },
  timestamp: { $gte: new Date(Date.now() - 604800000) }
})

// Find suspicious user deactivations
db.audit_logs.find({
  action: { $in: ["USER_DEACTIVATE", "USER_DELETE"] },
  timestamp: { $gte: new Date(Date.now() - 86400000) }
})
```

---

### Step 2: Contain (within 1 hour of detection)

**For compromised user account:**
1. Admin deactivates account immediately via `PUT /api/v1/org/members/{id}/status` with `is_active: false`
2. If student account — the `active_session_id` mechanism already prevents concurrent sessions
3. Rotate `SECRET_KEY` in `.env` — this invalidates ALL existing JWT tokens system-wide
4. Restart the server to apply new key

**For leaked API key (Azure / MongoDB / Stripe / Google):**
1. Immediately rotate the key in the respective service console
2. Update `.env` with new key
3. If GCP credentials leaked — regenerate `gcp-credentials.json` service account key in Google Cloud Console
4. Restart server

**For Stripe webhook compromise:**
1. Rotate `STRIPE_WEBHOOK_SECRET` in Stripe Dashboard
2. Update `.env` with new webhook secret
3. Restart server
4. Verify no unauthorized subscription changes occurred

**For suspected database breach:**
1. Revoke the MongoDB Atlas connection string
2. Create new credentials in Atlas
3. Update `MONGODB_URI` in `.env`
4. Restart server

**For server-level compromise:**
1. Take server offline
2. Preserve logs before shutdown: copy `app_server.log*` files
3. Rebuild from clean deployment using `deploy.sh`

---

### Step 3: Investigate (within 24 hours)

1. Query `audit_logs` for the affected time window
2. Identify `actor_id` of suspicious actions
3. Check `app_server.log` for IP addresses and error patterns
4. Review rate limiter's `PERMANENTLY_BANNED_IPS` for related IPs
5. Determine scope — what data was accessed or modified?
6. Check `transaction_logs` for unauthorized payment activity
7. Document timeline of events

---

### Step 4: Notify

| Who | When | How |
|---|---|---|
| Affected users | Within 72 hours of confirmed breach | Email via SMTP (Gmail) |
| If dental practice data involved | Per applicable regulations | Direct contact |
| Stripe (if payment data involved) | Immediately | Stripe support portal |

**Notification must include:**
- What happened
- What data was potentially affected
- What actions Petal AI has taken
- What the user should do (e.g., change password)

---

### Step 5: Recover

1. Restore service with patched/secured configuration
2. Re-enable affected user accounts if cleared
3. Verify `audit_logs` integrity post-recovery
4. Verify all background workers are running (processing worker, SOAP retention worker)
5. Monitor for 48 hours after recovery

---

### Step 6: Post-Incident Review (within 1 week)

- What was the root cause?
- How was it detected?
- How long did it take to contain?
- What controls failed?
- What changes are needed to prevent recurrence?
- Update this plan if needed

---

## 5. Key Credentials Location

> ⚠️ These must never be committed to Git. Stored in `.env` only.

| Credential | Environment Variable |
|---|---|
| JWT signing key | `SECRET_KEY` |
| MongoDB connection | `MONGODB_URI` |
| Azure Storage | `AZURE_STORAGE_CONNECTION_STRING` |
| Azure Storage Account Key | `AZURE_STORAGE_ACCOUNT_KEY` |
| Azure OpenAI | `AZURE_OPENAI_API_KEY` |
| Azure OpenAI Embedding | `AZURE_OPENAI_EMBEDDING_KEY` |
| Azure Search | `AZURE_SEARCH_KEY` |
| Azure Document Intelligence | `AZURE_DOCUMENT_INTELLIGENCE_KEY` |
| Azure Vision | `AZURE_VISION_KEY` |
| Azure Speech | `AZURE_SPEECH_KEY` |
| Google Cloud | `gcp-credentials.json` (file) + `GOOGLE_API_KEY` |
| Stripe | `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` |
| reCAPTCHA | `RECAPTCHA_SECRET_KEY` |
| Google OAuth | `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` |
| SMTP (Gmail) | `SMTP_USER` + `SMTP_PASSWORD` |

---

## 6. Emergency Contacts

| Contact | Role |
|---|---|
| Admin (internal) | Primary incident responder |
| MongoDB Atlas Support | `https://support.mongodb.com` |
| Azure Support | `https://portal.azure.com/#blade/Microsoft_Azure_Support` |
| Stripe Support | `https://support.stripe.com` |
| Google Cloud Support | `https://cloud.google.com/support` |

---

**Policy Owner:** Petal AI Admin  
**Last Reviewed:** 2026-10-12
