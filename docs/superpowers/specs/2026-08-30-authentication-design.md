# Authentication System Design

**Date:** 2026-08-30  
**Status:** Design Complete  
**Branch:** feat/authentication

## Overview

This document specifies the authentication system for OpenHire, enabling login/signup for candidates and recruiters with email + password credentials, JWT access tokens, and refresh token flow.

## Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Authentication Type | Email + Password | Traditional, secure, user-familiar |
| Session Management | JWT + Refresh Token Hybrid | Balance security (short-lived access) with UX (long-lived refresh) |
| Email Verification | Not Required | Faster onboarding for MVP |
| Role Assignment | User Selects During Signup | Clear separation, no admin overhead |
| Password Reset | Not Implemented | Users contact support (simpler MVP) |

## Architecture

```
User Signup/Login
    ↓
POST /auth/signup or /auth/login
    ↓
AuthService validates credentials
    ↓
JWT tokens generated (access + refresh)
    ↓
Tokens returned to client, stored in localStorage
    ↓
Client includes Access Token in Authorization header for all requests
    ↓
AuthProvider validates token on protected endpoints
    ↓
Token expires → Client auto-refreshes via /auth/refresh
```

## Database Schema

### Users Table
```sql
CREATE TABLE IF NOT EXISTS users (
    user_id         text PRIMARY KEY,              -- uuid format
    email           text NOT NULL UNIQUE,          -- login credential
    password_hash   text NOT NULL,                 -- bcrypt hashed
    user_type       text NOT NULL,                 -- 'candidate' or 'recruiter'
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
```

**Relationships:**
- Candidate signup creates `users` entry with `user_type='candidate'`
- Recruiter signup creates `users` entry with `user_type='recruiter'`
- `candidate.candidate_id` references `users.user_id`
- `recruiter.recruiter_id` references `users.user_id` (new)

### Refresh Tokens Table (Optional - for token revocation)
```sql
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_id        text PRIMARY KEY,
    user_id         text NOT NULL REFERENCES users(user_id),
    token_hash      text NOT NULL,
    expires_at      timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id ON refresh_tokens(user_id);
```

## Backend Implementation

### 1. AuthService (`services/auth_service.py`)

**Methods:**
- `signup(email: str, password: str, user_type: str) → dict`
  - Validate email format and uniqueness
  - Hash password with bcrypt
  - Create user record in DB
  - Generate and return access + refresh tokens

- `login(email: str, password: str) → dict`
  - Fetch user by email
  - Verify password hash
  - Generate and return access + refresh tokens
  - Error: UnauthorizedError if credentials invalid

- `refresh_access_token(refresh_token: str) → dict`
  - Validate refresh token signature
  - Verify token not expired
  - Generate new access token
  - Return new access token

- `verify_access_token(token: str) → Principal`
  - Decode JWT token
  - Validate signature and expiration
  - Extract user_id, email, user_type
  - Return Principal object
  - Error: UnauthorizedError if invalid/expired

**Token Structure:**

Access Token (expires in 15 minutes):
```json
{
  "user_id": "user_abc123",
  "email": "user@example.com",
  "user_type": "candidate",
  "exp": 1234567890,
  "iat": 1234567000,
  "type": "access"
}
```

Refresh Token (expires in 7 days):
```json
{
  "user_id": "user_abc123",
  "exp": 1234567890,
  "iat": 1234567000,
  "type": "refresh"
}
```

### 2. UserRepository (`repositories/postgres/repository.py`)

**Methods:**
- `save(user: UserRecord) → UserRecord`
  - Insert or update user in DB
  - Return saved record

- `get_by_email(email: str) → Optional[UserRecord]`
  - Fetch user by email
  - Return None if not found

- `get_by_id(user_id: str) → Optional[UserRecord]`
  - Fetch user by user_id
  - Return None if not found

**UserRecord Model:**
```python
class UserRecord(BaseModel):
    user_id: str
    email: str
    password_hash: str
    user_type: str  # 'candidate' or 'recruiter'
    is_active: bool = True
    created_at: datetime
    updated_at: Optional[datetime] = None
```

### 3. Auth Routes (`api/routes/auth.py` - new)

**Endpoints:**

`POST /auth/signup`
- Request: `{ email, password, user_type }`
- Response: `{ access_token, refresh_token, user: { user_id, email, user_type } }`
- Errors: 400 (invalid email/password), 409 (email exists)

`POST /auth/login`
- Request: `{ email, password }`
- Response: `{ access_token, refresh_token, user: { user_id, email, user_type } }`
- Errors: 401 (invalid credentials)

`POST /auth/refresh`
- Request: `{ refresh_token }`
- Response: `{ access_token }`
- Errors: 401 (invalid/expired refresh token)

### 4. AuthProvider Implementation

Implement `core.security.AuthProvider` to validate JWT tokens:
- Extract token from Authorization header
- Call `auth_service.verify_access_token(token)`
- Return Principal with user info
- Errors: UnauthorizedError if token invalid

## Frontend Implementation

### 1. Signup Page (`pages/signup.html`)

**Form Fields:**
- Email input
- Password input
- Role selector: Radio buttons ("I'm a Candidate" / "I'm a Recruiter")
- Sign Up button
- Link to login page

**Logic:**
1. Validate email format and password strength (client-side)
2. Call `POST /auth/signup` with email, password, user_type
3. On success: Store access + refresh tokens in localStorage
4. Redirect to dashboard (candidate or recruiter based on user_type)
5. On error: Show error message (email exists, invalid format, etc.)

### 2. Login Page (`pages/login.html`)

**Form Fields:**
- Email input
- Password input
- Login button
- Link to signup page

**Logic:**
1. Validate email format (client-side)
2. Call `POST /auth/login` with email, password
3. On success: Store access + refresh tokens in localStorage
4. Redirect to dashboard
5. On error: Show error message (invalid credentials)

### 3. Token Management (`pages/js/auth.js` - new)

**Functions:**
```javascript
// Get current access token
function getAccessToken() {
  return localStorage.getItem('access_token');
}

// Get refresh token
function getRefreshToken() {
  return localStorage.getItem('refresh_token');
}

// Store tokens after login/signup
function setTokens(accessToken, refreshToken) {
  localStorage.setItem('access_token', accessToken);
  localStorage.setItem('refresh_token', refreshToken);
}

// Logout: clear tokens and redirect
function logout() {
  localStorage.removeItem('access_token');
  localStorage.removeItem('refresh_token');
  window.location.href = 'login.html';
}

// Refresh access token when expired
async function refreshAccessToken() {
  const refreshToken = getRefreshToken();
  const response = await fetch('/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken })
  });
  
  if (response.ok) {
    const { access_token } = await response.json();
    localStorage.setItem('access_token', access_token);
    return access_token;
  } else {
    logout(); // Refresh failed, force logout
  }
}

// Login
async function login(email, password) {
  const response = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password })
  });
  
  if (response.ok) {
    const { access_token, refresh_token, user } = await response.json();
    setTokens(access_token, refresh_token);
    // Redirect based on user type
    const dashboard = user.user_type === 'recruiter' ? 'recruiter.html' : 'candidate.html';
    window.location.href = dashboard;
  } else {
    const error = await response.json();
    alert(`Login failed: ${error.detail}`);
  }
}

// Signup
async function signup(email, password, userType) {
  const response = await fetch('/auth/signup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, user_type: userType })
  });
  
  if (response.ok) {
    const { access_token, refresh_token, user } = await response.json();
    setTokens(access_token, refresh_token);
    // Redirect to dashboard
    const dashboard = user.user_type === 'recruiter' ? 'recruiter.html' : 'candidate.html';
    window.location.href = dashboard;
  } else {
    const error = await response.json();
    alert(`Signup failed: ${error.detail}`);
  }
}
```

### 4. API Request Enhancement (`pages/js/app.js`)

Update `apiRequest()` to:
1. Include `Authorization: Bearer {access_token}` header
2. Handle 401 responses:
   - Call `refreshAccessToken()`
   - Retry original request with new token
3. Handle 403 responses:
   - Redirect to login
4. Handle token expiration:
   - Decode token locally to check expiration
   - Pre-emptively refresh before expiry

```javascript
async function apiRequest(url, options = {}) {
  let token = getAccessToken();
  
  // Add Authorization header
  const headers = options.headers || {};
  headers['Authorization'] = `Bearer ${token}`;
  
  let response = await fetch(url, { ...options, headers });
  
  // If token expired (401), try to refresh
  if (response.status === 401) {
    token = await refreshAccessToken();
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
      response = await fetch(url, { ...options, headers });
    } else {
      // Refresh failed, logout
      logout();
      throw new Error('Authentication failed');
    }
  }
  
  if (!response.ok) {
    throw new Error(`API error: ${response.status}`);
  }
  
  return response.json();
}
```

### 5. Navigation Guard

On app load, check if user is authenticated:
```javascript
window.addEventListener('load', () => {
  const token = getAccessToken();
  const currentPage = window.location.pathname;
  
  // Public pages (login, signup)
  const publicPages = ['login.html', 'signup.html'];
  const isPublicPage = publicPages.some(p => currentPage.includes(p));
  
  if (!token && !isPublicPage) {
    // Not authenticated, redirect to login
    window.location.href = 'login.html';
  } else if (token && isPublicPage) {
    // Already authenticated, redirect to dashboard
    window.location.href = 'recruiter.html'; // or candidate.html
  }
});
```

## Integration with Existing Code

### 1. Update `core/security.py`
- Implement `AuthProvider` interface
- Use `AuthService.verify_access_token()` to extract Principal

### 2. Update `core/container.py`
- Inject AuthService into container
- Install custom AuthProvider instead of AnonymousAuthProvider

### 3. Update `core/config.py`
- Add JWT secret key configuration: `JWT_SECRET_KEY` env var
- Add token expiration times: `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`

### 4. Update `core/lifespan.py`
- Extend schema initialization to include users and refresh_tokens tables

### 5. Update `api/routes/candidates.py`
- Link candidate signup to user account creation
- Ensure `candidate.candidate_id` references `users.user_id`

### 6. Create `api/routes/recruiter.py` (if not exists)
- Link recruiter signup to user account creation
- Ensure `recruiter.recruiter_id` references `users.user_id`

## Security Considerations

1. **Password Hashing:** Use bcrypt with salt (cost factor 12)
2. **Token Signing:** Use HS256 (HMAC-SHA256) with strong secret key
3. **Token Expiration:** Access tokens expire in 15 minutes, refresh tokens in 7 days
4. **HTTPS Only:** Tokens must be sent over HTTPS in production
5. **Token Storage:** localStorage used for MVP (consider httpOnly cookies for future)
6. **CORS:** Configure CORS to allow frontend origin only
7. **Rate Limiting:** Consider adding rate limiting to /auth/login to prevent brute force (future)
8. **Secret Management:** JWT_SECRET_KEY should be environment variable, never hardcoded

## Error Handling

| Scenario | HTTP Status | Response |
|----------|-------------|----------|
| Invalid email format | 400 | `{ "detail": "Invalid email format" }` |
| Password too weak | 400 | `{ "detail": "Password must be at least 8 characters" }` |
| Email already exists | 409 | `{ "detail": "Email already registered" }` |
| Invalid credentials | 401 | `{ "detail": "Invalid email or password" }` |
| Token expired | 401 | `{ "detail": "Token expired" }` |
| Invalid token | 401 | `{ "detail": "Invalid token" }` |
| Refresh token invalid | 401 | `{ "detail": "Refresh token invalid or expired" }` |

## Testing Strategy

1. **Unit Tests:** AuthService methods (hash, verify, token generation)
2. **Integration Tests:** Auth routes with database
3. **E2E Tests:** Signup → Login → Protected request → Token refresh → Logout

## Future Enhancements

1. Email verification
2. Password reset via email
3. 2FA (Two-Factor Authentication)
4. OAuth2/Social login
5. Rate limiting on auth endpoints
6. Account lockout after failed login attempts
7. Session management (view active sessions, logout all devices)
8. Audit logging (login history, failed attempts)

## Files to Create/Modify

**New Files:**
- `services/auth_service.py`
- `repositories/postgres/user_repository.py`
- `api/routes/auth.py`
- `pages/login.html`
- `pages/signup.html`
- `pages/js/auth.js`

**Modified Files:**
- `core/security.py` (implement AuthProvider)
- `core/container.py` (inject AuthService)
- `core/config.py` (add JWT config)
- `core/lifespan.py` (extend schema)
- `repositories/postgres/schema.sql` (add users table)
- `pages/js/app.js` (add auth headers)
- `api/routes/candidates.py` (link to users)

## Deployment Checklist

- [ ] Set `JWT_SECRET_KEY` environment variable
- [ ] Set `ACCESS_TOKEN_EXPIRE_MINUTES` (default: 15)
- [ ] Set `REFRESH_TOKEN_EXPIRE_DAYS` (default: 7)
- [ ] Enable `AUTH_ENABLED=true`
- [ ] Run database schema migration (includes users table)
- [ ] Test signup/login flow end-to-end
- [ ] Test token refresh flow
- [ ] Test 401 handling in protected endpoints
