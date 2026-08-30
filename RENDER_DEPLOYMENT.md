# Render Deployment Guide for OpenHire Production

## Infrastructure Overview

```
Frontend:  https://openhire-sable.vercel.app/     (Vercel)
Backend:   https://openhire-xc9c.onrender.com     (Render)
Database:  PostgreSQL on Render (Singapore)
Region:    singapore-postgres.render.com
```

## Step 1: Verify Database Connection

Test the PostgreSQL connection is working:

```bash
# From your local machine, test the connection
psql postgresql://openhire_r9gz_user:WuEGZ5zwCZ8QNlCmDIJp62O5EUQ2yf0A@dpg-da9ahkcs728c73d6srqg-a.singapore-postgres.render.com/openhire_r9gz -c "SELECT version();"
```

If successful, you should see PostgreSQL version info.

---

## Step 2: Get Required API Keys

You need to obtain these keys for production:

### 1. Groq API Key (for LLM) - REQUIRED
- Go to: https://console.groq.com
- Sign up / Login
- Create API key
- Copy to: `GROQ_API_KEY`

### 2. Azure Speech Keys (OPTIONAL - for audio)
- Go to: https://portal.azure.com
- Create "Speech Services" resource
- Copy Key and Region to:
  - `AZURE_SPEECH_KEY`
  - `AZURE_SPEECH_REGION`

**For now, we'll use:**
- LLM: Groq
- Audio: Mock (text-only)
- TTS: Edge (free)

---

## Step 3: Generate JWT Secret Key

Generate a secure random key for JWT signing:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Example output: `o_BqVE8w-Z1xY2aB3cD4eF5gH6iJ7kL8mN9oP`

**Store this somewhere safe** - you'll need it in the next step.

---

## Step 4: Set Environment Variables on Render

### Go to Render Dashboard

1. Log in to https://dashboard.render.com
2. Select your **openhire-xc9c** service (backend)
3. Click **Settings** tab
4. Scroll to **Environment** section

### Add/Update Variables

Click **Add Environment Variable** for each of these:

```
ENVIRONMENT                  = production
DEBUG                       = false
APP_NAME                    = OpenHire Live Interview API
APP_VERSION                 = 0.1.0

LLM_PROVIDER                = groq
GROQ_API_KEY                = <YOUR_GROQ_API_KEY_HERE>
GROQ_MODEL                  = openai/gpt-oss-20b

AUDIO_PROVIDER              = mock
TTS_PROVIDER                = edge
EDGE_TTS_VOICE              = en-US-JennyNeural

DATABASE_URL                = postgresql://openhire_r9gz_user:WuEGZ5zwCZ8QNlCmDIJp62O5EUQ2yf0A@dpg-da9ahkcs728c73d6srqg-a.singapore-postgres.render.com/openhire_r9gz

AUTH_ENABLED                = true
AUTH_REQUIRED_BY_DEFAULT    = true
JWT_SECRET_KEY              = <YOUR_GENERATED_JWT_SECRET_KEY>
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS   = 7

CORS_ALLOW_ORIGINS          = https://openhire-sable.vercel.app
CORS_ALLOW_CREDENTIALS      = true
CORS_ALLOW_METHODS          = GET,POST,PUT,DELETE,OPTIONS
CORS_ALLOW_HEADERS          = Content-Type,Authorization
CORS_MAX_AGE                = 3600

LOG_LEVEL                   = INFO
LOG_JSON                    = true
LOG_ACCESS                  = true
LOG_QUERY_STRING            = false

EMBEDDING_PROVIDER          = local
EMBEDDING_MODEL             = all-MiniLM-L6-v2
VECTOR_STORE_TYPE           = faiss
VECTOR_STORE_PATH           = /data/faiss_index

MAX_REQUEST_BODY_BYTES      = 12582912
HOST                        = 0.0.0.0
PORT                        = 8000
API_PREFIX                  = 
SERVE_STATIC_PAGES          = true
```

### Important Notes

- **Replace placeholders:**
  - `<YOUR_GROQ_API_KEY_HERE>` → Your actual Groq API key
  - `<YOUR_GENERATED_JWT_SECRET_KEY>` → Your generated JWT key

- **CORS_ALLOW_ORIGINS**: Make sure it matches your frontend domain exactly
  - Frontend: `https://openhire-sable.vercel.app`

- **DATABASE_URL**: Already provided (PostgreSQL on Render)

---

## Step 5: Deploy to Render

After setting all environment variables:

1. Click **Deploy** button at top of Render dashboard
2. Or go to **Deploys** tab and click **Create Deploy**
3. Select **Latest Commit** or **Git Commit**
4. Wait for deployment to complete (2-5 minutes)

Check deployment status:
- Green checkmark = Success
- Red X = Failed (check logs)

---

## Step 6: Verify Production Deployment

### Test Health Endpoint

```bash
curl https://openhire-xc9c.onrender.com/health
```

Expected response:
```json
{"status": "ok"}
```

### Test Configuration Endpoint

```bash
curl https://openhire-xc9c.onrender.com/ | jq .
```

Expected response:
```json
{
  "service": "openhire-live-interview-api",
  "app": "OpenHire Live Interview API",
  "version": "0.1.0",
  "environment": "production",
  "debug": false,
  "llm_provider": "groq",
  "audio_provider": "mock",
  "tts_provider": "edge",
  "auth_enabled": true,
  "persistence": "durable"
}
```

**Key checks:**
- ✅ `environment: production` (not development)
- ✅ `auth_enabled: true` (authentication working)
- ✅ `persistence: durable` (database connected)
- ✅ `/docs` returns 404 (hidden in production)
- ✅ `/openapi.json` returns 404 (hidden in production)

### Test Docs Hidden

```bash
curl -I https://openhire-xc9c.onrender.com/docs
```

Should return: `404 Not Found`

### Test Auth Endpoints

```bash
# Signup
curl -X POST https://openhire-xc9c.onrender.com/auth/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "securepassword123",
    "user_type": "recruiter"
  }'
```

Expected response:
```json
{
  "user": {
    "user_id": "user_xxxxx",
    "email": "test@example.com",
    "user_type": "recruiter"
  },
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### Test Database Persistence

1. Create a user via `/auth/signup`
2. Stop/restart the service (Render will auto-restart on crash)
3. Query the same user - should still exist in database

---

## Step 7: Frontend Configuration

Update frontend to use production backend URL:

**In Vercel (https://vercel.com):**

1. Go to your **openhire-sable** project
2. Settings → Environment Variables
3. Set:
   ```
   VITE_API_URL = https://openhire-xc9c.onrender.com
   REACT_APP_API_URL = https://openhire-xc9c.onrender.com
   ```
4. Redeploy frontend

Or in your `.env.production` (frontend):
```env
VITE_API_URL=https://openhire-xc9c.onrender.com
REACT_APP_API_URL=https://openhire-xc9c.onrender.com
```

---

## Production Verification Checklist

### Pre-Deployment
- [ ] Groq API key obtained
- [ ] JWT secret key generated (32+ bytes)
- [ ] Database URL verified and working
- [ ] Frontend domain set in CORS_ALLOW_ORIGINS

### Post-Deployment
- [ ] `/health` returns 200 ✓
- [ ] `/` shows `environment: production` ✓
- [ ] `/docs` returns 404 ✓
- [ ] `/auth/signup` works and returns JWT ✓
- [ ] `/candidates` returns data ✓
- [ ] Database persists data ✓
- [ ] Frontend can connect to backend ✓

### Security
- [ ] DEBUG=false in production ✓
- [ ] CORS restricted to frontend domain ✓
- [ ] API keys stored as environment secrets (not in code) ✓
- [ ] JWT_SECRET_KEY is cryptographically random ✓
- [ ] HTTPS enforced (Render automatic) ✓

---

## Monitoring & Logs

### View Backend Logs

1. Render Dashboard → openhire-xc9c service
2. **Logs** tab
3. Filter by log level or search for errors

### View Database Logs

1. Render Dashboard → Your PostgreSQL instance
2. Check connection logs and query logs

### Set Up Error Tracking (Optional)

For production, consider:
- **Sentry**: Error tracking and monitoring
- **LogRocket**: User session replay
- **Datadog**: Full observability

---

## Troubleshooting

### "Unauthorized" or "401 Forbidden" errors
**Cause**: JWT secret key different from what signed the token
**Fix**: Ensure `JWT_SECRET_KEY` is identical everywhere

### "Database connection refused"
**Cause**: DATABASE_URL incorrect or network issue
**Fix**: Test locally: `psql <DATABASE_URL> -c "SELECT 1"`

### "CORS error from frontend"
**Cause**: Frontend domain not in CORS_ALLOW_ORIGINS
**Fix**: Update CORS_ALLOW_ORIGINS on Render and redeploy

### "Provider not found" error for Groq
**Cause**: GROQ_API_KEY empty or invalid
**Fix**: Verify key is set in Render env vars (not in code)

### "/docs still visible"
**Cause**: ENVIRONMENT not set to production
**Fix**: Ensure `ENVIRONMENT=production` in env vars

---

## Next Steps

1. ✅ Get Groq API key
2. ✅ Generate JWT secret key
3. ✅ Set all env vars on Render
4. ✅ Deploy and verify
5. ✅ Update frontend API URL
6. ✅ Test end-to-end (signup, create interview, score)
7. ⭐ Monitor production deployment

---

## Support

- **Backend Logs**: Render Dashboard → Logs
- **Database**: `psql <DATABASE_URL>`
- **API Docs**: Temporarily enable in dev: `ENVIRONMENT=development`
- **Frontend Issues**: Check browser console for CORS/API errors
