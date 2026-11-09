/**
 * Minimal & fast navigation and state handler
 */
// Prefer an explicit per-page override. Local pages use the same FastAPI origin
// so development-only routes such as /live-sessions/demo stay local.
const DEFAULT_RENDER_BACKEND = 'https://openhire.devsidd.tech';
const API_BASE = typeof window.OPENHIRE_API_URL === 'string'
  ? window.OPENHIRE_API_URL.replace(/\/+$/, '')
  : ((location.hostname === 'localhost' || location.hostname === '127.0.0.1')
      ? ''
      : DEFAULT_RENDER_BACKEND);

/**
 * Absolute URL that starts the LinkedIn OAuth flow.
 *
 * The API sets its `oauth_state` cookie on its own host and LinkedIn sends the
 * browser back to the API's registered callback, so this must never resolve
 * against a separately hosted static frontend (e.g. the Vercel deployment).
 * A relative link there 404s before OAuth even starts. Locally the pages are
 * served by the API itself, so API_BASE is empty and a path is enough.
 */
function linkedinAuthorizeUrl(role = 'candidate') {
  return `${API_BASE}/auth/linkedin/authorize?role=${encodeURIComponent(role)}`;
}

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
  // OAuth callback pages may start API work before their page-level guard
  // finishes exchanging the one-time ticket. Serialize all normal API calls
  // behind that initialization so they never race the token exchange.
  if (typeof initAuth === 'function' && !isAuthTokenIssuingPath(path)) {
    await initAuth();
  }

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
    title: job.title || 'Open role',
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
  const applications = data.applications || [];
  // Application records intentionally contain ids only. Resolve titles in a
  // single batch so the candidate dashboard can show the public role name.
  const jobs = await Promise.all(applications.map(async (application) => {
    try {
      const job = await fetchJob(application.job_id);
      return [application.job_id, job.title];
    } catch (err) {
      return [application.job_id, null];
    }
  }));
  const titles = Object.fromEntries(jobs);
  return applications.map(application => Object.assign({}, application, {
    job_title: titles[application.job_id] || null,
  }));
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

const OPENHIRE_LLM_PROVIDERS = [
  ['openai', 'OpenAI'], ['gemini', 'Gemini'], ['anthropic', 'Anthropic'],
  ['groq', 'Groq'], ['nvidia_nim', 'NVIDIA NIM'], ['openrouter', 'OpenRouter'],
];
const OPENHIRE_LLM_MODELS = {
  openai: ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini'],
  gemini: ['gemini-3.5-flash-lite', 'gemini-3.8-flash', 'gemini-3.5-flash', 'gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.1-flash-lite'],
  anthropic: ['claude-3-5-sonnet-latest', 'claude-3-5-haiku-latest', 'claude-3-7-sonnet-latest'],
  groq: ['openai/gpt-oss-20b', 'llama-3.3-70b-versatile'],
  nvidia_nim: ['nvidia/nemotron-3-super-120b-a12b'],
  openrouter: ['openai/gpt-4o-mini', 'anthropic/claude-3.5-sonnet'],
};

/** Mount account-level BYOK preferences in an AI workflow. */
async function mountLlmWorkflowSettings(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return { save: async () => {} };
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  try {
    const [credentials, settings] = await Promise.all([
      apiRequest('/auth/me/llm-credentials'),
      apiRequest('/auth/me/llm-settings'),
    ]);
    let provider = settings.provider || (credentials.find(c => c.credential_id === settings.default_credential_id) || {}).provider || OPENHIRE_LLM_PROVIDERS[0][0];
    let credentialId = settings.default_credential_id || '';
    let model = settings.model || '';
    let modeValue = settings.mode || 'platform';
    const key = () => credentials.find(c => c.credential_id === credentialId);
    const label = () => modeValue === 'platform' ? 'OpenHire Platform' : `${OPENHIRE_LLM_PROVIDERS.find(p => p[0] === provider)?.[1] || provider} - ${model || 'default model'}${key() ? ` - ${key().name || key().key_hint || 'saved key'}` : ''}`;
    container.innerHTML = `<div class="byok-workflow-control"><label class="form-label" for="${containerId}Mode">AI provider mode</label><select class="form-control" id="${containerId}Mode"><option value="platform">Platform</option><option value="byok">BYOK</option></select><div class="byok-workflow-summary"><span id="${containerId}Summary"></span><button type="button" class="btn-outline btn-sm" id="${containerId}Change" hidden>Change</button></div><span class="form-status" id="${containerId}Status" aria-live="polite"></span></div>`;
    const mode = container.querySelector(`#${containerId}Mode`);
    const summary = container.querySelector(`#${containerId}Summary`);
    const change = container.querySelector(`#${containerId}Change`);
    const status = container.querySelector(`#${containerId}Status`);
    mode.value = modeValue;
    const sync = () => { summary.textContent = label(); change.hidden = modeValue !== 'byok'; };
    sync();
    let overlay = document.getElementById(`${containerId}Modal`);
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = `${containerId}Modal`;
      overlay.className = 'modal-overlay byok-modal-overlay';
      overlay.hidden = true;
      document.body.appendChild(overlay);
    }
    const openModal = () => {
      const opts = OPENHIRE_LLM_PROVIDERS.map(([value, text]) => `<option value="${value}" ${value === provider ? 'selected' : ''}>${text}</option>`).join('');
      overlay.innerHTML = `<div class="modal byok-modal" role="dialog" aria-modal="true"><div class="modal-header"><div><h2 class="modal-title">Choose AI provider</h2><p class="card-sub">Select a provider, saved API key and model for this operation.</p></div><button type="button" class="btn-ghost modal-close" data-close aria-label="Close">&times;</button></div><div class="modal-body"><div class="form-group"><label class="form-label">Provider</label><select class="form-control" data-provider>${opts}</select></div><div class="form-group"><label class="form-label">Saved API key</label><select class="form-control" data-key></select></div><div class="form-group"><label class="form-label">Model</label><select class="form-control" data-model></select></div><div class="form-status" data-status aria-live="polite"></div></div><div class="byok-modal-actions"><button type="button" class="btn-outline" data-close>Cancel</button><button type="button" class="btn-primary" data-apply>Use selection</button></div></div>`;
      const providerSelect = overlay.querySelector('[data-provider]');
      const credentialSelect = overlay.querySelector('[data-key]');
      const modelSelect = overlay.querySelector('[data-model]');
      const refresh = reset => {
        const available = credentials.filter(item => item.provider === providerSelect.value && item.status === 'active');
        credentialSelect.innerHTML = available.length ? available.map(item => `<option value="${esc(item.credential_id)}">${esc(item.name || item.key_hint || item.credential_id)}</option>`).join('') : '<option value="">No active keys saved</option>';
        credentialSelect.value = !reset && available.some(item => item.credential_id === credentialId) ? credentialId : (available[0]?.credential_id || '');
        const selected = available.find(item => item.credential_id === credentialSelect.value);
        const models = OPENHIRE_LLM_MODELS[providerSelect.value] || [];
        const chosenModel = !reset && model ? model : (selected?.model || models[0] || '');
        modelSelect.innerHTML = models.map(item => `<option value="${esc(item)}">${esc(item)}</option>`).join('');
        if (chosenModel && !models.includes(chosenModel)) modelSelect.insertAdjacentHTML('afterbegin', `<option value="${esc(chosenModel)}">${esc(chosenModel)}</option>`);
        modelSelect.value = chosenModel;
        credentialSelect.disabled = !available.length;
      };
      refresh(false);
      providerSelect.onchange = () => refresh(true);
      credentialSelect.onchange = () => {
        credentialId = credentialSelect.value;
        model = (credentials.find(item => item.credential_id === credentialId) || {}).model || model;
        refresh(false);
      };
      overlay.querySelectorAll('[data-close]').forEach(button => { button.onclick = () => { overlay.hidden = true; }; });
      overlay.onclick = event => { if (event.target === overlay) overlay.hidden = true; };
      overlay.querySelector('[data-apply]').onclick = () => {
        const selected = credentials.find(item => item.credential_id === credentialSelect.value);
        if (!selected) {
          overlay.querySelector('[data-status]').textContent = 'Add an active key from Profile - API Keys to use BYOK.';
          return;
        }
        provider = providerSelect.value;
        credentialId = selected.credential_id;
        model = modelSelect.value;
        modeValue = 'byok';
        mode.value = 'byok';
        sync();
        overlay.hidden = true;
      };
      overlay.hidden = false;
    };
    mode.onchange = () => { modeValue = mode.value; sync(); if (modeValue === 'byok') openModal(); else overlay.hidden = true; };
    change.onclick = openModal;
    const save = async () => {
      if (modeValue === 'byok' && !key()) throw new Error('Select an active API key in the BYOK settings dialog.');
      status.textContent = 'Saving AI settings...';
      status.className = 'form-status';
      await apiRequest('/auth/me/llm-settings', { method: 'PUT', body: {
        mode: modeValue,
        provider,
        default_credential_id: modeValue === 'byok' ? credentialId : null,
        fallback_to_other_keys: settings.fallback_to_other_keys !== false,
        fallback_to_platform: settings.fallback_to_platform !== false,
        model: modeValue === 'byok' ? model : null,
      }});
      status.textContent = 'AI settings saved.';
      status.className = 'form-status success';
    };
    return { save };
  } catch (err) {
    container.innerHTML = `<p class="card-sub">AI provider settings could not load: ${esc(err.message)}</p>`;
    return { save: async () => { throw new Error('AI provider settings could not be loaded.'); } };
  }
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

async function createInterviewWithMode(path, body) {
  return apiRequest(path, { method: 'POST', body });
}

/** Creates or repairs the interview session for a shortlisted application.
 * Gemini Live is the only interview transport. If an application still
 * points at a pre-Live session, the backend creates and links a replacement
 * Gemini Live session while preserving the old record for auditability. */
async function ensureInterviewSession(application) {
  if (application.session_id) {
    try {
      await apiRequest(`/live-sessions/${encodeURIComponent(application.session_id)}`);
      return application.session_id;
    } catch (error) {
      if (error?.status !== 404) throw error;
      // The linked record is an older adaptive/turn-based session. Continue
      // below so the server can relink this application to Gemini Live.
    }
  }
  const [job, candidate] = await Promise.all([
    apiRequest(`/jobs/${encodeURIComponent(application.job_id)}`),
    apiRequest(`/candidates/${encodeURIComponent(application.candidate_id)}`),
  ]);
  const body = {
    candidate_id: application.candidate_id,
    job_id: application.job_id,
    job_description: job.job,
    parsed_resume: candidate.resume,
    application_id: application.application_id,
  };
  const session = await createInterviewWithMode('/live-sessions', body);
  return session.session_id;
}

// The interview page can repair a stale link when opened directly from an
// old application URL, without duplicating the creation logic.
globalThis.ensureInterviewSession = ensureInterviewSession;

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

async function logoutUser() {
  try {
    await fetch('/auth/logout', { method: 'POST', credentials: 'include' });
  } catch (err) {
    // Proceed with client logout even if network request fails
  }
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
        <img class="brand-mark" src="assets/openhire.svg" alt="OpenHire">
        OpenHire
      </div>

      <nav class="nav-links">
        <a href="index.html">Overview</a>
        ${!isAdmin ? `<a href="${dashboardHref}" class="${activePage === 'dashboard' ? 'active' : ''}">Dashboard</a>` : ''}
        ${!isRecruiter && !isAdmin ? `<a href="mock-interviews.html" class="${activePage === 'mock-interviews' ? 'active' : ''}">Mock Interviews</a><a href="jobs.html" class="${activePage === 'jobs' ? 'active' : ''}">Jobs</a>` : ''}
        ${isRecruiter ? `<a href="create-job.html" class="${activePage === 'create-job' ? 'active' : ''}">+ Job</a>` : ''}
        ${isRecruiter ? `<a href="screening.html" class="${activePage === 'screening' ? 'active' : ''}">Screening</a>` : ''}
        <a href="leaderboard.html" class="${activePage === 'leaderboard' ? 'active' : ''}">Leaderboard</a>
        <a href="openbox.html" class="${activePage === 'openbox' ? 'active' : ''}">OpenBox</a>
        ${isAdmin ? `<a href="admin.html" class="${activePage === 'admin' ? 'active' : ''}">Admin</a>` : ''}
      </nav>

      <div class="nav-right">
        <button type="button" class="theme-toggle" data-theme-toggle aria-pressed="false" onclick="toggleTheme()" aria-label="Switch to dark theme" title="Switch to dark theme">
          <i class="ph-bold ph-moon" aria-hidden="true"></i>
        </button>
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

  if (typeof updateThemeToggle === 'function') updateThemeToggle();

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

/**
 * Scroll-entry reveals. `transform` + `opacity` only, run through a single
 * IntersectionObserver (never a scroll listener). Children of a
 * `.reveal-stagger` container inherit a `--i` index used for the 60ms
 * cascade delay in CSS. Respects prefers-reduced-motion by revealing
 * everything instantly.
 */
function initReveal() {
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!('IntersectionObserver' in window)) {
    document.querySelectorAll('.reveal').forEach((el) => el.classList.add('visible'));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
  const seen = new WeakSet();
  const observeEl = (el) => {
    if (seen.has(el) || el.classList.contains('visible')) return;
    seen.add(el);
    const stagger = el.parentElement && el.parentElement.classList.contains('reveal-stagger');
    if (stagger) {
      const idx = Array.prototype.indexOf.call(el.parentElement.children, el);
      el.style.setProperty('--i', String(idx));
    } else if (el.dataset.revealDelay) {
      el.style.setProperty('--i', el.dataset.revealDelay);
    }
    io.observe(el);
  };
  if (reduce) {
    document.querySelectorAll('.reveal').forEach((el) => el.classList.add('visible'));
    return;
  }
  document.querySelectorAll('.reveal').forEach(observeEl);
  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.classList && node.classList.contains('reveal')) observeEl(node);
        if (node.querySelectorAll) node.querySelectorAll('.reveal').forEach(observeEl);
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initReveal);
} else {
  initReveal();
}
