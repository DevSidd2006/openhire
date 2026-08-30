# OpenHire Production Deployment Guide

This guide walks through setting up OpenHire for production deployment.

## Quick Start

1. **Create production config**: Copy `.env.production.example` to `.env.production`
2. **Choose providers**: Decide on LLM, Audio, and TTS providers
3. **Set up database**: Provision PostgreSQL instance
4. **Generate secrets**: Create secure JWT key and API keys
5. **Deploy**: Push to production environment with env vars

---

## Step 1: Choose & Configure Providers

### LLM Provider

**Recommendation: Groq** (fastest, most affordable, recommended in codebase)

```env
LLM_PROVIDER=groq
GROQ_API_KEY=<get from https://console.groq.com>
GROQ_MODEL=openai/gpt-oss-20b
```

Alternative providers:
- **OpenAI**: `LLM_PROVIDER=openai` + `OPENAI_API_KEY`
- **Gemini**: `LLM_PROVIDER=gemini` + `GEMINI_API_KEY`

### Audio Provider (Speech-to-Text)

**Options:**
1. **Azure Speech** (recommended for quality)
   ```env
   AUDIO_PROVIDER=azure
   AZURE_SPEECH_KEY=<from Azure>
   AZURE_SPEECH_REGION=eastus
   ```

2. **Groq Whisper** (reuses Groq key)
   ```env
   AUDIO_PROVIDER=groq
   # (reuses GROQ_API_KEY)
   ```

3. **Mock** (text-only interviews, no audio)
   ```env
   AUDIO_PROVIDER=mock
   ```

### Text-to-Speech Provider

**Recommendation: Edge TTS** (free, no API key)

```env
TTS_PROVIDER=edge
EDGE_TTS_VOICE=en-US-JennyNeural
```

Alternatives:
- **Azure Speech**: `TTS_PROVIDER=azure` (reuse Azure credentials)
- **Mock**: `TTS_PROVIDER=mock` (text-only)

---

## Step 2: Set Up PostgreSQL Database

The application requires PostgreSQL for persistent data storage in production.

### Option A: Render PostgreSQL (Current Host)
If already using Render for backend hosting:

1. Go to Render dashboard → PostgreSQL
2. Create new PostgreSQL database
3. Copy connection string to `DATABASE_URL`

Example URL: `postgresql://user:password@dpg-xxxx.render.internal:5432/openhire`

### Option B: AWS RDS
```
postgresql://admin:password@openhire.xxxx.us-east-1.rds.amazonaws.com:5432/openhire
```

### Option C: Google Cloud SQL
```
postgresql://postgres:password@xx.xx.xx.xx:5432/openhire
```

### Option D: DigitalOcean Managed Database
```
postgresql://doadmin:password@db-xxxx-do-user-xxxx.db.ondigitalocean.com:25060/openhire
```

### Option E: Railway
```
postgresql://postgres:password@containers.railway.app:6432/railway
```

**Environment Variable:**
```env
DATABASE_URL=postgresql://user:password@host:port/openhire
```

---

## Step 3: Generate Secrets

### JWT Secret Key (CRITICAL)

Generate a cryptographically random 32+ byte key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Example output: `o_BqVE8w-Z1xY2aB3cD4eF5gH6iJ7kL8mN9oP`

Set in environment:
```env
JWT_SECRET_KEY=<output-from-above>
```

**Important:**
- Never commit to git
- Different value per environment (dev, staging, prod)
- Store in secrets manager (Render Env Vars, AWS Secrets Manager, etc.)

### API Keys

Securely store these provider keys as environment variables:
- `GROQ_API_KEY` - from https://console.groq.com
- `OPENAI_API_KEY` - from OpenAI dashboard (if using)
- `GEMINI_API_KEY` - from Google AI Studio (if using)
- `AZURE_SPEECH_KEY` - from Azure portal (if using)

Never:
- Hardcode them in `.env` checked into git
- Commit `.env.production` file
- Log them in application output

---

## Step 4: Configure CORS

Set allowed frontend origins for your deployment:

```env
CORS_ALLOW_ORIGINS=https://yourdomain.com,https://www.yourdomain.com
CORS_ALLOW_CREDENTIALS=true
```

**Production Rules:**
- Always use HTTPS (never HTTP in production)
- List exact domain names (never wildcard `*`)
- Include all variations (www, api, app subdomains)

---

## Step 5: Configure Authentication

### Enable Authentication

```env
AUTH_ENABLED=true
AUTH_REQUIRED_BY_DEFAULT=true
```

### Token Expiration

```env
ACCESS_TOKEN_EXPIRE_MINUTES=15      # Access token valid for 15 minutes
REFRESH_TOKEN_EXPIRE_DAYS=7         # Refresh token valid for 7 days
```

**Workflow:**
1. User calls `/auth/signup` or `/auth/login` → receives access + refresh tokens
2. Access token expires after 15 min
3. Frontend calls `/auth/refresh` with refresh token to get new access token
4. Refresh token expires after 7 days → user must login again

---

## Step 6: Logging Configuration

```env
ENVIRONMENT=production
DEBUG=false
LOG_LEVEL=INFO
LOG_JSON=true
LOG_ACCESS=true
LOG_QUERY_STRING=false
```

**What this does:**
- `ENVIRONMENT=production`: Hides OpenAPI docs (`/docs` returns 404)
- `DEBUG=false`: Production error responses (no stack traces)
- `LOG_LEVEL=INFO`: Only log info and above (no debug spew)
- `LOG_JSON=true`: Structured JSON logs for log aggregation services
- `LOG_ACCESS=false`: Skip noisy access logs (optional)
- `LOG_QUERY_STRING=false`: Don't log auth tokens in query strings

---

## Step 7: Render Deployment

### Set Environment Variables on Render

1. Go to your Render service dashboard
2. Navigate to **Environment** section
3. Add all variables from `.env.production`:

```
ENVIRONMENT=production
DEBUG=false
LLM_PROVIDER=groq
GROQ_API_KEY=<your-key>
AUDIO_PROVIDER=azure
AZURE_SPEECH_KEY=<your-key>
AZURE_SPEECH_REGION=eastus
TTS_PROVIDER=edge
DATABASE_URL=<your-postgres-url>
AUTH_ENABLED=true
JWT_SECRET_KEY=<your-secure-key>
CORS_ALLOW_ORIGINS=https://yourdomain.com
... (rest of vars)
```

4. Click **Save** and redeploy

### Verify Deployment

Once deployed, verify production configuration:

```bash
# Health check
curl https://openhire-xc9c.onrender.com/health

# Configuration summary (should NOT show /docs or /openapi.json in prod)
curl https://openhire-xc9c.onrender.com/ | jq .

# Verify /docs is hidden in production
curl -I https://openhire-xc9c.onrender.com/docs
# Should return 404 if ENVIRONMENT=production
```

---

## Step 8: Database Initialization

If this is the first time connecting to the database:

1. No migration framework configured yet - tables will be created automatically by SQLAlchemy on first run
2. Verify connection works:
   ```bash
   python3 -c "from api.app import app; print('Connection OK')"
   ```

Future: When migrations are needed, use Alembic:
```bash
alembic upgrade head
```

---

## Verification Checklist

### Pre-Deployment

- [ ] `.env.production.example` is checked into git (template only)
- [ ] `.env.production` is in `.gitignore` (secrets file)
- [ ] All API keys obtained and ready
- [ ] Database provisioned and connection tested
- [ ] JWT secret key generated (32+ bytes)
- [ ] CORS origins configured for your frontend domain
- [ ] Environment set to `production`
- [ ] Debug mode set to `false`

### Post-Deployment

- [ ] `/health` returns 200 OK
- [ ] `/` shows production configuration
- [ ] `/docs` returns 404 (hidden in production)
- [ ] `/auth/signup` works and returns JWT tokens
- [ ] `/candidates` accessible and returns data
- [ ] Database persists data across restarts
- [ ] All HTTPS (no mixed content)
- [ ] CORS headers correct on cross-origin requests
- [ ] No API keys in error messages or logs

### Monitoring

- [ ] Error logs monitored (set up Sentry, DataDog, etc.)
- [ ] API latency tracked
- [ ] Database connection pool healthy
- [ ] Rate limiting in place (if needed)
- [ ] Backups automated

---

## Troubleshooting

### "DATABASE_URL is empty" error
- **Cause**: Database URL not set in environment
- **Fix**: Set `DATABASE_URL=postgresql://...` in Render env vars

### "/docs still visible in production"
- **Cause**: `ENVIRONMENT` not set to `production`
- **Fix**: Set `ENVIRONMENT=production` in env vars

### "Authorization failed" errors
- **Cause**: JWT_SECRET_KEY differs from what signed the token
- **Fix**: Ensure same `JWT_SECRET_KEY` is used everywhere (don't regenerate)

### "CORS error from frontend"
- **Cause**: Frontend domain not in `CORS_ALLOW_ORIGINS`
- **Fix**: Add frontend URL to `CORS_ALLOW_ORIGINS` and redeploy

### "Connection refused: database"
- **Cause**: Database URL unreachable or credentials wrong
- **Fix**: Test URL: `psql <DATABASE_URL>`

---

## Security Best Practices

1. **Secrets Management**
   - Use platform secrets (Render, AWS Secrets Manager)
   - Never commit `.env.production`
   - Rotate JWT keys periodically

2. **Database**
   - Enable SSL/TLS on database connections
   - Use strong passwords
   - Enable automated backups
   - Restrict network access (allow only backend IP)

3. **API Keys**
   - Restrict API key scopes (if provider allows)
   - Monitor usage for anomalies
   - Rotate compromised keys immediately

4. **Logging**
   - Don't log sensitive data (API keys, passwords, PII)
   - Enable log aggregation and alerting
   - Archive logs securely

5. **Monitoring**
   - Set up error tracking (Sentry, Rollbar)
   - Monitor database connection pool
   - Alert on unusual traffic patterns

---

## Next Steps

1. **Prepare credentials**: Get API keys from providers
2. **Provision database**: Create PostgreSQL instance
3. **Generate secrets**: Create JWT key and secure values
4. **Update `.env.production.example`** with your actual configuration
5. **Deploy to Render**: Set env vars and redeploy
6. **Test thoroughly**: Verify all features work
7. **Set up monitoring**: Enable error tracking and logs

---

## Support & Resources

- **Groq API Docs**: https://console.groq.com/docs
- **OpenAI API**: https://platform.openai.com/docs
- **Google Gemini**: https://ai.google.dev/docs
- **Azure Speech**: https://learn.microsoft.com/en-us/azure/cognitive-services/speech-service/
- **PostgreSQL Docs**: https://www.postgresql.org/docs/
- **Render Docs**: https://render.com/docs
