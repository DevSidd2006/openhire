/**
 * Shared state & helpers for OpenHire pages
 */
const API_BASE = '/api';

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

function logout() {
  localStorage.removeItem('openhire_user');
  window.location.href = 'auth.html';
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
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>
        Open<span>Hire</span>
      </div>
      
      <div class="nav-links">
        <a href="index.html">Overview</a>
        <a href="${dashboardHref}" class="${activePage === 'dashboard' ? 'active' : ''}">Dashboard</a>
        ${isRecruiter ? '<a href="create-job.html">Post Job</a>' : ''}
        <a href="leaderboard.html" class="${activePage === 'leaderboard' ? 'active' : ''}">Leaderboard</a>
      </div>

      <div class="nav-right">
        ${user ? `
          <div class="user-badge">
            <span>${user.name}</span>
            <span class="role-tag">${user.role}</span>
          </div>
          <button class="btn-outline" onclick="logout()">Logout</button>
        ` : `
          <button class="btn-outline" onclick="window.location.href='auth.html'">Sign In</button>
        `}
      </div>
    </header>
  `;
}

// Initial mock jobs store
function getStoredJobs() {
  const local = localStorage.getItem('openhire_jobs');
  if (local) {
    try { return JSON.parse(local); } catch (e) {}
  }
  return [
    {
      job_id: "backend-lead-01",
      title: "Senior Backend Architect",
      description: "Looking for an engineer experienced with Python, FastAPI, distributed caching, and microservice architectures.",
      competencies: ["Python", "System Design", "FastAPI", "PostgreSQL", "Concurrency"]
    },
    {
      job_id: "frontend-eng-02",
      title: "Lead Frontend Engineer",
      description: "Expertise in React, TypeScript, WebSocket streaming, and state management.",
      competencies: ["TypeScript", "React", "WebSockets", "CSS Architecture"]
    }
  ];
}

function saveJobs(jobs) {
  localStorage.setItem('openhire_jobs', JSON.stringify(jobs));
}

function getStoredApplications() {
  const local = localStorage.getItem('openhire_applications');
  if (local) {
    try { return JSON.parse(local); } catch (e) {}
  }
  return [];
}

function saveApplications(apps) {
  localStorage.setItem('openhire_applications', JSON.stringify(apps));
}
