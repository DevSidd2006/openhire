/**
 * Minimal & fast navigation and state handler
 */
// The backend URL. When hosted on Vercel or locally, connects to the deployed Render backend
// unless running on the same origin (e.g. backend serving /app).
const DEFAULT_RENDER_BACKEND = 'https://openhire-xc9c.onrender.com';
const API_BASE = window.OPENHIRE_API_URL || (
  location.hostname === 'localhost' ||
  location.hostname === '127.0.0.1' ||
  location.hostname === 'onrender.com' ||
  location.hostname.endsWith('.onrender.com')
    ? ''
    : DEFAULT_RENDER_BACKEND
);

/** Helper to construct WebSocket URL for the session, resolving against API_BASE or location.host */
function getWebSocketUrl(path) {
  if (API_BASE && API_BASE.startsWith('http')) {
    const wsProto = API_BASE.startsWith('https') ? 'wss:' : 'ws:';
    const host = API_BASE.replace(/^https?:\/\//, '').replace(/\/+$/, '');
    return `${wsProto}//${host}${path.startsWith('/') ? '' : '/'}${path}`;
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}${path.startsWith('/') ? '' : '/'}${path}`;
}


/**
 * Thin fetch wrapper shared by every real backend call in this frontend.
 * JSON-encodes a plain-object body (and sets Content-Type: application/json
 * for it); a `FormData` body (multipart file uploads - see
 * pages/apply.html's resume upload) is passed through untouched, with NO
 * Content-Type header set, so the browser can add its own `multipart/
 * form-data; boundary=...` - setting that header manually is a classic way
 * to silently corrupt a multipart upload. Throws a plain Error with a
 * human-readable `.message` (never a raw stack trace) on any network or
 * HTTP failure, and returns the parsed JSON body on success.
 *
 * Includes Authorization header with Bearer token if available.
 * Handles 401 responses by attempting to refresh the token and retrying.
 */
// Token-issuing endpoints only - these must NOT receive an Authorization
// header (and must not trigger a 401-refresh-and-retry), or a stale/expired
// token could interfere with login/signup/refresh itself. All other /auth*
// endpoints (e.g. /auth/me, /auth/me/password) are normal authenticated
// endpoints and need the token like any other route.
const AUTH_TOKEN_ISSUING_PATHS = ['/auth/login', '/auth/signup', '/auth/refresh'];
function isAuthTokenIssuingPath(path) {
  return AUTH_TOKEN_ISSUING_PATHS.some((p) => path.startsWith(p));
}

async function apiRequest(path, options = {}) {
  const opts = Object.assign({}, options);
  const isFormData = typeof FormData !== 'undefined' && opts.body instanceof FormData;
  opts.headers = isFormData
    ? Object.assign({}, options.headers || {})
    : Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});

  // Add Authorization header if we have an access token
  // Skip for auth endpoints to avoid circular refresh attempts
  if (typeof getAccessToken === 'function' && !isAuthTokenIssuingPath(path)) {
    const token = getAccessToken();
    if (token) {
      opts.headers['Authorization'] = `Bearer ${token}`;
    }
  }

  if (opts.body && !isFormData && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
  }

  let res;
  try {
    res = await fetch(API_BASE + path, opts);
  } catch (err) {
    throw new Error('Cannot reach the OpenHire backend. Please check that the server is running.');
  }

  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { /* non-JSON body */ }
  }

  // Handle 401 Unauthorized: try to refresh token and retry
  if (res.status === 401 && !isAuthTokenIssuingPath(path) && typeof refreshAccessToken === 'function') {
    try {
      const newToken = await refreshAccessToken();
      // Retry the request with the new token
      opts.headers['Authorization'] = `Bearer ${newToken}`;
      try {
        res = await fetch(API_BASE + path, opts);
      } catch (err) {
        throw new Error('Cannot reach the OpenHire backend. Please check that the server is running.');
      }

      const retryText = await res.text();
      let retryData = null;
      if (retryText) {
        try { retryData = JSON.parse(retryText); } catch (e) { /* non-JSON body */ }
      }

      if (!res.ok) {
        const detail = (retryData && (retryData.detail || retryData.error)) || `Request failed (HTTP ${res.status})`;
        const error = new Error(detail);
        error.status = res.status;
        error.data = retryData;
        throw error;
      }

      return retryData;
    } catch (refreshError) {
      // Refresh failed, logout and redirect
      if (typeof logout === 'function') {
        logout();
      }
      throw new Error('Your session has expired. Please sign in again.');
    }
  }

  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || `Request failed (HTTP ${res.status})`;
    const error = new Error(detail);
    error.status = res.status;
    error.data = data;
    throw error;
  }

  return data;
}

/** Flattens a JobResponse (job_id/job/is_active/...) into the flat shape
 * these pages render - job.title, job.description, job.competencies as a
 * plain array of names. */
function normalizeJob(record) {
  const job = record.job || {};
  return {
    job_id: record.job_id,
    title: job.title || record.job_id,
    description: job.description || '',
    competencies: (job.competencies || []).map((c) => c.name),
    is_active: record.is_active,
  };
}

async function fetchJobs() {
  const data = await apiRequest('/jobs');
  return (data.jobs || []).map(normalizeJob);
}

async function fetchJob(jobId) {
  const record = await apiRequest(`/jobs/${encodeURIComponent(jobId)}`);
  return normalizeJob(record);
}

async function fetchApplicationsForCandidate(candidateId) {
  const data = await apiRequest(`/applications?candidate_id=${encodeURIComponent(candidateId)}`);
  return data.applications || [];
}

/** GET /auth/me - the signed-in user's full profile. */
async function fetchMyProfile() {
  return apiRequest('/auth/me');
}

/** PATCH /auth/me with a partial set of fields. Pass only the fields that
 * changed; omit a field entirely to leave it alone, or pass `null` to
 * clear it - both are handled by the backend's exclude_unset semantics
 * (schemas/auth.py:UpdateProfileRequest). */
async function updateMyProfile(fields) {
  return apiRequest('/auth/me', { method: 'PATCH', body: fields });
}

/** POST /auth/me/password. Resolves with no value on success (204); throws
 * (via apiRequest's existing error handling) with a human-readable message
 * on a wrong current password (401) or a too-short new password (422). */
async function changeMyPassword(currentPassword, newPassword) {
  return apiRequest('/auth/me/password', {
    method: 'POST',
    body: { current_password: currentPassword, new_password: newPassword },
  });
}

/** GET /admin/users with optional {query, user_type, is_active} filters -
 * only ever called from pages/admin.html, only ever reachable by an admin
 * (the navbar link is hidden otherwise, and the backend 403s regardless). */
async function fetchAdminUsers(filters = {}) {
  const params = new URLSearchParams();
  if (filters.query) params.set('query', filters.query);
  if (filters.user_type) params.set('user_type', filters.user_type);
  if (filters.is_active !== undefined && filters.is_active !== '') {
    params.set('is_active', filters.is_active);
  }
  const qs = params.toString();
  return apiRequest(`/admin/users${qs ? '?' + qs : ''}`);
}

/** PATCH /admin/users/{userId} with a partial {is_active, user_type} body. */
async function updateAdminUser(userId, fields) {
  return apiRequest(`/admin/users/${userId}`, { method: 'PATCH', body: fields });
}

/** GET /admin/jobs, optionally including hidden (inactive) jobs. */
async function fetchAdminJobs(includeHidden = false) {
  return apiRequest(`/admin/jobs?include_hidden=${includeHidden}`);
}
/** PATCH /admin/jobs/{jobId} to set is_active. */
async function setAdminJobActive(jobId, isActive) {
  return apiRequest(`/admin/jobs/${jobId}`, { method: 'PATCH', body: { is_active: isActive } });
}
/** GET /admin/candidates, optionally including hidden candidates. */
async function fetchAdminCandidates(includeHidden = false) {
  return apiRequest(`/admin/candidates?include_hidden=${includeHidden}`);
}
/** PATCH /admin/candidates/{candidateId} to set is_hidden. */
async function setAdminCandidateHidden(candidateId, isHidden) {
  return apiRequest(`/admin/candidates/${candidateId}`, { method: 'PATCH', body: { is_hidden: isHidden } });
}
/** GET /admin/applications, optionally including hidden applications. */
async function fetchAdminApplications(includeHidden = false) {
  return apiRequest(`/admin/applications?include_hidden=${includeHidden}`);
}
/** PATCH /admin/applications/{applicationId} to set is_hidden. */
async function setAdminApplicationHidden(applicationId, isHidden) {
  return apiRequest(`/admin/applications/${applicationId}`, { method: 'PATCH', body: { is_hidden: isHidden } });
}

/** GET /admin/metrics - aggregate counts for the admin Metrics tab. */
async function fetchAdminMetrics() {
  return apiRequest('/admin/metrics');
}

/** GET /admin/system/status - live backend health for the admin Status tab
 * (persistence backend, provider config, a live DB ping, JWT
 * placeholder-secret flag, evaluation queue depth). */
async function fetchAdminSystemStatus() {
  return apiRequest('/admin/system/status');
}

/** Creates the interview session for an application that is already
 * SHORTLISTED and has none yet - fetches the full job/resume records
 * POST /sessions needs (it takes the full JobDescription/ParsedResume, not
 * just their ids - see api/models.py:CreateSessionRequest), and links it
 * to the application in the same call. Returns the session_id. */
async function ensureInterviewSession(application) {
  if (application.session_id) {
    return application.session_id;
  }
  const [job, candidate] = await Promise.all([
    apiRequest(`/jobs/${encodeURIComponent(application.job_id)}`),
    apiRequest(`/candidates/${encodeURIComponent(application.candidate_id)}`),
  ]);
  const session = await apiRequest('/sessions', {
    method: 'POST',
    body: {
      candidate_id: application.candidate_id,
      job_id: application.job_id,
      job_description: job.job,
      parsed_resume: candidate.resume,
      application_id: application.application_id,
      // The backend default (MAX_QUESTIONS_PER_INTERVIEW=12,
      // config/settings.py) is realistic for a real interview but too long
      // for a live demo click-through - keep this a short, deliberate demo
      // constant, not a behavior change to the adaptive engine itself.
      max_questions: 5,
    },
  });
  return session.session_id;
}

function getCurrentUser() {
  const user = localStorage.getItem('openhire_user');
  if (user) {
    try { return JSON.parse(user); } catch (e) {}
  }
  return { name: 'Alex Morgan', role: 'candidate' };
}

function setCurrentUser(user) {
  localStorage.setItem('openhire_user', JSON.stringify(user));
  renderNavbar();
}

/**
 * Re-resolves this candidate's candidate_id from the backend (GET
 * /candidates/me, keyed off the authenticated user_id) and merges it into
 * the stored user object if found.
 *
 * Fixes a real bug: candidate_id used to be knowable to the frontend ONLY
 * as a side effect of apply.html's POST /candidates succeeding, which
 * cached it into the stored user object right there. Logging back in later
 * called setCurrentUser({name, role, user_id}) with no candidate_id,
 * silently wiping the cached value - so a returning candidate's own
 * dashboard had nothing to query applications with and showed zero, even
 * though the application still existed server-side. Safe to call whenever
 * a candidate page loads and candidate_id might be missing; a 404 (no
 * candidate profile registered yet - a normal state before a first resume
 * upload) is swallowed, not surfaced as an error.
 */
async function resolveCandidateId() {
  const user = getCurrentUser();
  if (user.candidate_id) return user;
  try {
    const data = await apiRequest('/candidates/me');
    const updated = Object.assign({}, user, { candidate_id: data.candidate_id });
    setCurrentUser(updated);
    return updated;
  } catch (err) {
    return user;
  }
}

function logoutUser() {
  localStorage.removeItem('openhire_user');
  localStorage.removeItem('openhire_access_token');
  localStorage.removeItem('openhire_refresh_token');
  localStorage.removeItem('openhire_impersonating_as_admin');
  window.location.href = 'login.html';
}

/** POST /admin/impersonate/{userId}, swap stored tokens to the target
 * user's, remember which admin email started this so the banner (below)
 * can show it, and redirect to that user's landing page. */
async function impersonateUser(userId, targetEmail, targetRole) {
  const adminEmail = getCurrentUser() ? getCurrentUser().name : null;
  const response = await apiRequest(`/admin/impersonate/${userId}`, { method: 'POST' });
  setTokens(response.access_token, response.refresh_token);
  setCurrentUser({ name: targetEmail, role: targetRole, user_id: userId });
  if (adminEmail) {
    localStorage.setItem('openhire_impersonating_as_admin', adminEmail);
  }
  window.location.href = targetRole === 'admin'
    ? 'admin.html'
    : targetRole === 'recruiter'
      ? 'recruiter.html'
      : 'candidate.html';
}

/** Whether the CURRENT session is an impersonated one - set only by
 * impersonateUser() above, cleared only by returnFromImpersonation(). */
function isImpersonating() {
  return !!localStorage.getItem('openhire_impersonating_as_admin');
}

/** "Return to admin" - the simplest correct behavior for phase 1 (spec
 * §6/§8): re-authenticate as the admin normally, rather than retaining the
 * admin's original tokens across the impersonation session. */
function returnFromImpersonation() {
  localStorage.removeItem('openhire_impersonating_as_admin');
  logoutUser();
}

function renderNavbar(activePage = '') {
  const user = getCurrentUser();
  const navContainer = document.getElementById('navbarContainer');
  if (!navContainer) return;

  const isRecruiter = user && user.role === 'recruiter';
  const isAdmin = user && user.role === 'admin';
  const dashboardHref = isRecruiter ? 'recruiter.html' : 'candidate.html';

  navContainer.innerHTML = `
    <header class="navbar">
      <div class="brand-logo" onclick="window.location.href='index.html'">
        <svg class="brand-mark" viewBox="0 0 120 120" aria-hidden="true">
          <rect x="56" y="6" width="8" height="34" fill="var(--primary)"></rect>
          <rect x="56" y="80" width="8" height="34" fill="var(--primary)"></rect>
          <rect x="6" y="56" width="34" height="8" fill="var(--primary)"></rect>
          <rect x="80" y="56" width="34" height="8" fill="var(--primary)"></rect>
          <rect x="30" y="30" width="8" height="8" fill="var(--primary)"></rect>
          <rect x="82" y="30" width="8" height="8" fill="var(--primary)"></rect>
          <rect x="30" y="82" width="8" height="8" fill="var(--primary)"></rect>
          <rect x="82" y="82" width="8" height="8" fill="var(--primary)"></rect>
          <rect x="44" y="44" width="8" height="32" fill="var(--bg)"></rect>
          <rect x="68" y="44" width="8" height="32" fill="var(--bg)"></rect>
          <rect x="52" y="56" width="16" height="8" fill="var(--bg)"></rect>
        </svg>
        OpenHire
      </div>

      <nav class="nav-links">
        <a href="index.html">Overview</a>
        ${!isAdmin ? `<a href="${dashboardHref}" class="${activePage === 'dashboard' ? 'active' : ''}">Dashboard</a>` : ''}
        ${isRecruiter ? `<a href="create-job.html" class="${activePage === 'create-job' ? 'active' : ''}">+ Job</a>` : ''}
        ${isRecruiter ? `<a href="screening.html" class="${activePage === 'screening' ? 'active' : ''}">Screening</a>` : ''}
        <a href="leaderboard.html" class="${activePage === 'leaderboard' ? 'active' : ''}">Leaderboard</a>
        <a href="openbox.html" class="${activePage === 'openbox' ? 'active' : ''}">OpenBox</a>
        ${isAdmin ? `<a href="admin.html" class="${activePage === 'admin' ? 'active' : ''}">Admin</a>` : ''}
      </nav>

      <div class="nav-right">
        ${user ? `
          <div class="nav-menu">
            <button type="button" class="user-avatar nav-menu-trigger" id="userAvatarTrigger" onclick="toggleNavMenu(event)" aria-label="Account menu"></button>
            <div class="nav-menu-dropdown" id="navMenuDropdown" hidden>
              <a href="profile.html">Profile</a>
              <a href="profile.html#api-keys">API Keys</a>
              <a href="https://forms.gle/H31Jz9UVUzJejvcQ6" target="_blank" rel="noopener noreferrer">Feedback</a>
              <div class="nav-menu-divider"></div>
              <button type="button" class="nav-menu-item-btn" onclick="logoutUser()">Sign out</button>
            </div>
          </div>
        ` : `
          <button class="btn-outline" onclick="window.location.href='login.html'">Sign In</button>
        `}
      </div>
    </header>
  `;

  const avatarTrigger = document.getElementById('userAvatarTrigger');
  if (avatarTrigger && user) {
    // user.name is user-controlled - set via DOM properties, not interpolated
    // into the HTML template string above, per the XSS-safety convention.
    avatarTrigger.textContent = (user.name || '?').trim().charAt(0).toUpperCase();
    avatarTrigger.title = user.name;
  }

  const bannerContainer = document.getElementById('impersonationBanner');
  if (bannerContainer) {
    const adminEmail = localStorage.getItem('openhire_impersonating_as_admin');
    bannerContainer.innerHTML = '';
    if (user && adminEmail) {
      // user.name is a user-controlled string (target email) - build the
      // banner with textContent/createElement rather than interpolating it
      // into an HTML template string, per the XSS-safety convention from
      // Tasks 12/16.
      const banner = document.createElement('div');
      banner.className = 'impersonation-banner';
      banner.appendChild(document.createTextNode(`Viewing as ${user.name} — `));
      const returnBtn = document.createElement('button');
      returnBtn.type = 'button';
      returnBtn.className = 'link-btn';
      returnBtn.textContent = 'Return to admin';
      returnBtn.addEventListener('click', () => returnFromImpersonation());
      banner.appendChild(returnBtn);
      bannerContainer.appendChild(banner);
    }
  }
}

/** Toggles the top-right nav dropdown. Closes on an outside click or
 * Escape, and re-attaches those listeners each time it opens rather than
 * once at page load, since renderNavbar rebuilds this DOM from scratch on
 * every call (e.g. after setCurrentUser). */
function toggleNavMenu(event) {
  event.stopPropagation();
  const dropdown = document.getElementById('navMenuDropdown');
  if (!dropdown) return;

  const opening = dropdown.hidden;
  dropdown.hidden = !opening;
  if (!opening) return;

  const closeOnOutsideClick = (e) => {
    if (!dropdown.contains(e.target)) {
      dropdown.hidden = true;
      document.removeEventListener('click', closeOnOutsideClick);
      document.removeEventListener('keydown', closeOnEscape);
    }
  };
  const closeOnEscape = (e) => {
    if (e.key === 'Escape') {
      dropdown.hidden = true;
      document.removeEventListener('click', closeOnOutsideClick);
      document.removeEventListener('keydown', closeOnEscape);
    }
  };
  document.addEventListener('click', closeOnOutsideClick);
  document.addEventListener('keydown', closeOnEscape);
}

