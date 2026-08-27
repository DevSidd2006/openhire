/**
 * Minimal & fast navigation and state handler
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
        <span class="brand-dot"></span>OpenHire
      </div>
      
      <nav class="nav-links">
        <a href="index.html">Overview</a>
        <a href="${dashboardHref}" class="${activePage === 'dashboard' ? 'active' : ''}">Dashboard</a>
        ${isRecruiter ? `<a href="create-job.html" class="${activePage === 'create-job' ? 'active' : ''}">+ Job</a>` : ''}
        <a href="leaderboard.html" class="${activePage === 'leaderboard' ? 'active' : ''}">Leaderboard</a>
      </nav>

      <div class="nav-right">
        ${user ? `
          <div class="user-badge">
            <span>${user.name}</span>
            <span class="role-tag">${user.role}</span>
          </div>
          <button class="btn-outline" style="padding:0.25rem 0.6rem; font-size:11px;" onclick="logout()">Exit</button>
        ` : `
          <button class="btn-outline" onclick="window.location.href='auth.html'">Sign In</button>
        `}
      </div>
    </header>
  `;
}

function getStoredJobs() {
  const local = localStorage.getItem('openhire_jobs');
  if (local) {
    try { return JSON.parse(local); } catch (e) {}
  }
  return [
    {
      job_id: "backend-lead-01",
      title: "Senior Backend Architect",
      description: "FastAPI, distributed caching, PostgreSQL, high-concurrency systems design.",
      competencies: ["Python", "System Design", "FastAPI", "PostgreSQL"]
    },
    {
      job_id: "frontend-eng-02",
      title: "Lead Frontend Engineer",
      description: "TypeScript, React, WebSockets audio streaming, performant UI architecture.",
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
