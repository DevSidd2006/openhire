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
 * Clear all authentication tokens and redirect to login.
 */
function logout() {
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
function guardAuthenticatedPage() {
  const token = getAccessToken();
  if (!token) {
    window.location.href = 'login.html';
  }
}
