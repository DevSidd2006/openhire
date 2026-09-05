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
async function apiRequest(path, options = {}) {
  const opts = Object.assign({}, options);
  const isFormData = typeof FormData !== 'undefined' && opts.body instanceof FormData;
  opts.headers = isFormData
    ? Object.assign({}, options.headers || {})
    : Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});

  // Add Authorization header if we have an access token
  // Skip for auth endpoints to avoid circular refresh attempts
  if (typeof getAccessToken === 'function' && !path.startsWith('/auth')) {
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
  if (res.status === 401 && !path.startsWith('/auth') && typeof refreshAccessToken === 'function') {
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
  window.location.href = 'login.html';
}

function renderNavbar(activePage = '') {
  const user = getCurrentUser();
  const navContainer = document.getElementById('navbarContainer');
  if (!navContainer) return;

  const isRecruiter = user && user.role === 'recruiter';
  const dashboardHref = isRecruiter ? 'recruiter.html' : 'candidate.html';

  navContainer.innerHTML = `
    <header class="navbar">
      <div class="brand-logo" onclick="window.location.href='index.html'">
        <span class="brand-dot"></span>OpenHire
      </div>

      <nav class="nav-links">
        <a href="index.html">Overview</a>
        <a href="${dashboardHref}" class="${activePage === 'dashboard' ? 'active' : ''}">Dashboard</a>
        ${isRecruiter ? `<a href="create-job.html" class="${activePage === 'create-job' ? 'active' : ''}">+ Job</a>` : ''}
        ${isRecruiter ? `<a href="screening.html" class="${activePage === 'screening' ? 'active' : ''}">Screening</a>` : ''}
        <a href="leaderboard.html" class="${activePage === 'leaderboard' ? 'active' : ''}">Leaderboard</a>
        <a href="openbox.html" class="${activePage === 'openbox' ? 'active' : ''}">OpenBox</a>
      </nav>

      <div class="nav-right">
        ${user ? `
          <div class="nav-menu">
            <button type="button" class="user-badge nav-menu-trigger" onclick="toggleNavMenu(event)">
              <span>${user.name}</span>
              <span class="role-tag">${user.role}</span>
            </button>
            <div class="nav-menu-dropdown" id="navMenuDropdown" hidden>
              <a href="profile.html">Profile</a>
              <a href="profile.html#api-keys">API Keys</a>
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

