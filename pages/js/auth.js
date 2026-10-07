/**
 * Token management and authentication utilities.
 * 
 * Provides functions for:
 * - Token storage and retrieval
 * - Token expiration checking
 * - Automatic token refresh
 * - Logout functionality
 */

/**
 * Get the access token from localStorage.
 * @returns {string|null} The access token or null if not found
 */
function getAccessToken() {
  return localStorage.getItem('openhire_access_token');
}

/**
 * Get the refresh token from localStorage.
 * @returns {string|null} The refresh token or null if not found
 */
function getRefreshToken() {
  return localStorage.getItem('openhire_refresh_token');
}

/**
 * Store both access and refresh tokens in localStorage.
 * @param {string} accessToken - The JWT access token
 * @param {string} refreshToken - The JWT refresh token
 */
function setTokens(accessToken, refreshToken) {
  localStorage.setItem('openhire_access_token', accessToken);
  localStorage.setItem('openhire_refresh_token', refreshToken);
}

/**
 * Clear all authentication tokens, expire server cookies, and redirect to login.
 */
async function logout() {
  try {
    await fetch('/auth/logout', { method: 'POST', credentials: 'include' });
  } catch (err) {
    // Proceed with client logout even if network request fails
  }
  localStorage.removeItem('openhire_access_token');
  localStorage.removeItem('openhire_refresh_token');
  localStorage.removeItem('openhire_user');
  localStorage.removeItem('openhire_impersonating_as_admin');
  window.location.href = 'login.html';
}

/**
 * Decode a JWT token (client-side) without verification.
 * WARNING: This only validates the format and decodes the payload.
 * Do NOT rely on this for security - tokens are verified server-side.
 * 
 * @param {string} token - The JWT token to decode
 * @returns {object|null} The decoded payload or null if invalid
 */
function decodeJWT(token) {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    
    // Add padding if needed
    const payload = parts[1];
    const padded = payload + '='.repeat((4 - payload.length % 4) % 4);
    const decoded = atob(padded);
    return JSON.parse(decoded);
  } catch (err) {
    return null;
  }
}

/**
 * Check if the access token is expired.
 * @returns {boolean} True if expired or not found, false if still valid
 */
function isAccessTokenExpired() {
  const token = getAccessToken();
  if (!token) return true;
  
  const payload = decodeJWT(token);
  if (!payload || !payload.exp) return true;
  
  // Check if token expires within the next 60 seconds (with 1 minute buffer)
  const now = Math.floor(Date.now() / 1000);
  return payload.exp - now <= 60;
}

/**
 * Refresh the access token using the refresh token.
 * @returns {Promise<string>} The new access token
 * @throws {Error} If refresh fails
 */
async function refreshAccessToken() {
  const refreshToken = getRefreshToken();
  if (!refreshToken) {
    throw new Error('No refresh token available');
  }

  try {
    const response = await apiRequest('/auth/refresh', {
      method: 'POST',
      body: {
        refresh_token: refreshToken,
      },
    });
    
    const newAccessToken = response.access_token;
    
    // Store the new access token (keep the same refresh token)
    setTokens(newAccessToken, refreshToken);
    
    return newAccessToken;
  } catch (error) {
    // If refresh fails, clear tokens and redirect to login
    logout();
    throw new Error('Token refresh failed: ' + error.message);
  }
}

/**
 * Ensure the access token is valid, refreshing if necessary.
 * @returns {Promise<string>} A valid access token
 * @throws {Error} If refresh fails
 */
async function ensureValidAccessToken() {
  if (isAccessTokenExpired()) {
    return await refreshAccessToken();
  }
  return getAccessToken();
}

/**
 * Guard a page: redirect to login if not authenticated.
 * Call this at the top of pages that require authentication.
 */
/**
 * Guard a page: redirect to login if not authenticated.
 * Awaits OAuth ticket exchange completion if one is pending.
 */
async function guardAuthenticatedPage() {
  await initAuth();
  const token = getAccessToken();
  if (!token) {
    window.location.href = 'login.html';
    return false;
  }
  return true;
}

/** Recruiter-only pages still need a client-side guard because static pages
 * can be opened directly even when their API calls are role-protected. */
async function guardRecruiterPage() {
  const authed = await guardAuthenticatedPage();
  if (!authed) return false;
  const user = typeof getCurrentUser === 'function' ? getCurrentUser() : JSON.parse(localStorage.getItem('openhire_user') || 'null');
  if (user && user.role !== 'recruiter' && user.role !== 'admin') {
    window.location.href = 'candidate.html';
    return false;
  }
  return true;
}

/**
 * Automatically capture one-time ticket or tokens from OAuth redirects, exchange if needed,
 * store tokens, and clean the address bar. Prevents credentials from leaking in URLs.
 * Uses native fetch so it can run immediately before app.js is even parsed.
 */
async function captureOAuthTokensFromUrl() {
  const urlParams = new URLSearchParams(window.location.search);
  const ticket = urlParams.get('ticket');
  const accessToken = urlParams.get('access_token');
  const refreshToken = urlParams.get('refresh_token');

  // Immediately remove query params from browser history/address bar
  if (ticket || accessToken) {
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  // 1. One-time ticket exchange (native fetch, completely independent of app.js)
  if (ticket) {
    try {
      const res = await fetch('/auth/linkedin/exchange', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticket }),
        credentials: 'include',
      });
      if (res.ok) {
        const response = await res.json();
        setTokens(response.access_token, response.refresh_token);
        const userData = {
          name: response.user.email,
          role: response.user.user_type,
          user_id: response.user.user_id,
        };
        localStorage.setItem('openhire_user', JSON.stringify(userData));
        if (typeof setCurrentUser === 'function') {
          setCurrentUser(userData);
        }
        if (response.user.user_type === 'candidate' && typeof resolveCandidateId === 'function') {
          await resolveCandidateId();
        }
      } else {
        console.error('OAuth ticket exchange failed with HTTP status:', res.status);
      }
    } catch (err) {
      console.error('OAuth ticket exchange failed:', err);
    }
    return;
  }

  // 2. Direct tokens fallback
  if (accessToken && refreshToken) {
    setTokens(accessToken, refreshToken);
    const payload = decodeJWT(accessToken);
    if (payload) {
      const existingUser = localStorage.getItem('openhire_user');
      let userData = existingUser ? JSON.parse(existingUser) : {};
      userData.user_id = payload.sub || payload.user_id;
      userData.role = payload.user_type || payload.role;
      localStorage.setItem('openhire_user', JSON.stringify(userData));
    }
  }
}

let _authInitPromise = null;

function initAuth() {
  if (!_authInitPromise) {
    _authInitPromise = captureOAuthTokensFromUrl();
  }
  return _authInitPromise;
}

// Start OAuth exchange immediately when script executes
initAuth();
