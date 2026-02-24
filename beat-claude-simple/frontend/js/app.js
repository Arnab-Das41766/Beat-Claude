// Beat Claude - Main JavaScript Application

const API_URL = `${window.location.origin}/api`;

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);

const show = (el) => { if (el) el.classList.remove('hidden'); };
const hide = (el) => { if (el) el.classList.add('hidden'); };

const formatDate = (dateStr) => {
  if (!dateStr) return '-';
  return new Date(dateStr).toLocaleDateString();
};

const formatDateTime = (dateStr) => {
  if (!dateStr) return '-';
  return new Date(dateStr).toLocaleString();
};

const formatTime = (seconds) => {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
};

const formatPercent = (val) => {
  if (val == null) return '--%';
  return parseFloat(val).toFixed(1) + '%';
};

const getInitials = (name) => {
  if (!name) return '??';
  return name.split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase();
};

// Show alert message inline
const showAlert = (container, message, type = 'error') => {
  if (!container) return;
  container.innerHTML = `<div class="alert alert-${type}">${message}</div>`;
  setTimeout(() => container.innerHTML = '', 5000);
};

// Toast notification
const showToast = (message, type = 'success') => {
  // Remove existing toasts
  $$('.toast-notification').forEach(t => t.remove());

  const toast = document.createElement('div');
  toast.className = `toast-notification toast-${type}`;
  toast.textContent = message;
  toast.style.cssText = `
    position: fixed;
    bottom: 1.5rem;
    right: 1.5rem;
    background: ${type === 'error' ? '#ef4444' : type === 'warning' ? '#f59e0b' : '#10b981'};
    color: white;
    padding: 0.875rem 1.25rem;
    border-radius: 0.625rem;
    font-size: 0.875rem;
    font-weight: 500;
    box-shadow: 0 10px 30px rgba(0,0,0,0.15);
    z-index: 9999;
    animation: slideIn 0.3s ease;
    max-width: 320px;
  `;

  document.body.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transition = 'opacity 0.3s';
    setTimeout(() => toast.remove(), 300);
  }, 3000);
};

// Show loading spinner in a container
const showLoading = (container) => {
  if (container) container.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
};

const hideLoading = (container) => {
  if (container) { const l = container.querySelector('.loading'); if (l) l.remove(); }
};

// ============================================================================
// API HELPER
// ============================================================================

const api = {
  async get(endpoint) {
    const response = await fetch(`${API_URL}${endpoint}`, {
      credentials: 'include'
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Request failed' }));
      throw new Error(err.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },

  async post(endpoint, data) {
    const options = {
      method: 'POST',
      credentials: 'include'
    };

    if (data instanceof FormData) {
      options.body = data;
    } else {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(data);
    }

    const response = await fetch(`${API_URL}${endpoint}`, options);
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Request failed' }));
      throw new Error(err.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },

  async delete(endpoint) {
    const response = await fetch(`${API_URL}${endpoint}`, {
      method: 'DELETE',
      credentials: 'include'
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Request failed' }));
      throw new Error(err.detail || `HTTP ${response.status}`);
    }
    return response.json();
  }
};

// ============================================================================
// AUTH FUNCTIONS
// ============================================================================

const auth = {
  _cached: null,

  async check() {
    try {
      const data = await api.get('/me');
      if (data.success) {
        this._cached = data.user;
        return data.user;
      }
      this._cached = null;
      return null;
    } catch {
      this._cached = null;
      return null;
    }
  },

  async login(email, password) {
    const formData = new FormData();
    formData.append('email', email);
    formData.append('password', password);
    const result = await api.post('/login', formData);
    if (result.user) this._cached = result.user;
    return result;
  },

  async register(email, password, fullName, companyName = '') {
    const formData = new FormData();
    formData.append('email', email);
    formData.append('password', password);
    formData.append('full_name', fullName);
    formData.append('company_name', companyName);
    return api.post('/register', formData);
  },

  async logout() {
    this._cached = null;
    return api.post('/logout', {});
  }
};

// ============================================================================
// ASSESSMENT FUNCTIONS
// ============================================================================

const assessments = {
  async list() {
    return api.get('/assessments');
  },

  async get(id) {
    return api.get(`/assessments/${id}`);
  },

  async create(jdText, numQuestions = 10, duration = 60) {
    const formData = new FormData();
    formData.append('jd_text', jdText);
    formData.append('num_questions', numQuestions);
    formData.append('duration', duration);
    return api.post('/assessments', formData);
  },

  async publish(id) {
    return api.post(`/assessments/${id}/publish`, {});
  },

  async close(id) {
    return api.post(`/assessments/${id}/close`, {});
  },

  async delete(id) {
    return api.delete(`/assessments/${id}`);
  }
};

// ============================================================================
// RESULTS FUNCTIONS
// ============================================================================

const results = {
  async get(assessmentId) {
    return api.get(`/assessments/${assessmentId}/results`);
  },

  async leaderboard(assessmentId) {
    return api.get(`/assessments/${assessmentId}/leaderboard`);
  },

  async candidateDetails(candidateId) {
    return api.get(`/candidates/${candidateId}/details`);
  },

  async score(candidateId) {
    return api.post(`/score/${candidateId}`, {});
  }
};

// Dashboard
const dashboard = {
  async get() {
    return api.get('/dashboard');
  }
};

// ============================================================================
// CANDIDATE (PUBLIC) FUNCTIONS
// ============================================================================

const candidates = {
  async getPublicAssessment(assessmentId) {
    const response = await fetch(`${API_URL}/public/assessments/${assessmentId}`);
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Assessment not found' }));
      throw new Error(err.detail || 'Assessment not available');
    }
    return response.json();
  },

  async register(assessmentId, fullName, email, phone = '') {
    const formData = new FormData();
    formData.append('assessment_id', assessmentId);
    formData.append('full_name', fullName);
    formData.append('email', email);
    formData.append('phone', phone);
    return api.post('/candidates/register', formData);
  },

  async getTest(candidateId) {
    return api.get(`/candidates/${candidateId}/test`);
  },

  async submit(candidateId, answers, timeSpentMinutes) {
    return api.post(`/candidates/${candidateId}/submit`, {
      answers,
      time_spent_minutes: timeSpentMinutes
    });
  },

  async getStatus(candidateId) {
    return api.get(`/candidates/${candidateId}/status`);
  }
};

// ============================================================================
// NAVIGATION & AUTH GUARDS
// ============================================================================

const navigateTo = (page) => {
  window.location.href = page;
};

// Async auth check - returns user or redirects to login
const requireAuth = async () => {
  const user = await auth.check();
  if (!user) {
    navigateTo('/pages/login.html');
    return null;
  }
  return user;
};

// checkAuth - alias used by some pages (legacy support)
const checkAuth = () => {
  // This is a compatibility shim - pages that call checkAuth() synchronously
  // should use requireAuth() instead. Pages using this will still work via
  // async init() pattern, but this prevents ReferenceError crashes.
  return auth._cached;
};

// ============================================================================
// NAVIGATION RENDERING
// ============================================================================

const renderNavbar = async () => {
  const user = await auth.check();
  const navbar = $('.navbar-content');

  if (!navbar) return;

  let navHtml = `
    <a href="${user ? '/pages/dashboard.html' : '/'}" class="logo">
      <div class="logo-icon">🧠</div>
      <span class="logo-text">Beat Claude</span>
    </a>
    <div class="nav-links">
  `;

  if (user) {
    navHtml += `
      <span style="color: var(--slate-600); font-size: 0.875rem;">${user.full_name}</span>
      <button class="btn btn-ghost" onclick="handleLogout()">Logout</button>
    `;
  } else {
    navHtml += `
      <a href="/pages/login.html" class="btn btn-ghost">Sign In</a>
      <a href="/pages/register.html" class="btn btn-primary">Get Started</a>
    `;
  }

  navHtml += '</div>';
  navbar.innerHTML = navHtml;
};

// Logout handler
const handleLogout = async () => {
  await auth.logout();
  navigateTo('/');
};

// Initialize page
const initPage = async () => {
  await renderNavbar();
};

// Run on page load
document.addEventListener('DOMContentLoaded', initPage);