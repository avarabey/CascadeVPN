const API_ROOT = '/api';

export class ApiError extends Error {
  constructor(message, status = 0, body = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

const sessionState = {
  authenticated: false,
  csrfToken: '',
  user: null,
};

function updateSession(data = {}) {
  const authenticated = Boolean(data.authenticated ?? data.is_authenticated ?? data.user);
  sessionState.authenticated = authenticated;
  sessionState.csrfToken = authenticated ? (data.csrf_token ?? data.csrfToken ?? sessionState.csrfToken ?? '') : '';
  sessionState.user = authenticated ? (data.user ?? data.username ?? sessionState.user ?? null) : null;
  document.dispatchEvent(new CustomEvent('portal:session', { detail: { ...sessionState } }));
  return { ...sessionState };
}

async function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  headers.set('Accept', 'application/json');

  if (options.body != null && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && sessionState.csrfToken) {
    headers.set('X-CSRF-Token', sessionState.csrfToken);
  }

  let response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      ...options,
      method,
      headers,
      credentials: 'same-origin',
      body: options.body == null || options.body instanceof FormData
        ? options.body
        : JSON.stringify(options.body),
    });
  } catch (error) {
    throw new ApiError('Сервер сейчас недоступен', 0, error);
  }

  let data = null;
  if (response.status !== 204) {
    const type = response.headers.get('content-type') || '';
    try {
      data = type.includes('application/json') ? await response.json() : await response.text();
    } catch {
      data = null;
    }
  }

  if (!response.ok) {
    if (response.status === 401) updateSession({ authenticated: false, csrf_token: '' });
    const message = data?.detail || data?.message || data?.error?.message || data?.error?.code || response.statusText || 'Ошибка запроса';
    throw new ApiError(String(message), response.status, data);
  }
  return data;
}

function listFrom(data, keys) {
  if (Array.isArray(data)) return data;
  for (const key of keys) {
    if (Array.isArray(data?.[key])) return data[key];
  }
  return [];
}

export const api = {
  get session() {
    return { ...sessionState };
  },

  async getSession() {
    try {
      return updateSession(await request('/auth/session'));
    } catch (error) {
      if (error.status === 401 || error.status === 404) return updateSession({ authenticated: false, csrf_token: '' });
      throw error;
    }
  },

  async login(credentials) {
    const data = await request('/auth/login', { method: 'POST', body: credentials });
    return updateSession({ authenticated: true, ...data });
  },

  async logout() {
    try {
      await request('/auth/logout', { method: 'POST' });
    } catch (error) {
      if (error.status === 401) return updateSession({ authenticated: false });
      throw error;
    }
    return updateSession({ authenticated: false });
  },

  health: () => request('/health'),

  async getServices() {
    const data = await request('/status');
    return {
      services: listFrom(data, ['services', 'items', 'results']),
      summary: data?.summary || null,
      checkedAt: data?.checked_at || data?.updated_at || null,
    };
  },
  addService: (service) => request('/status/services', { method: 'POST', body: service }),
  deleteService: (id) => request(`/status/services/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  checkServices: (id) => request('/status/check', { method: 'POST', body: id == null ? {} : { id } }),

  async getFeeds() {
    const data = await request('/feeds');
    return listFrom(data, ['feeds', 'items', 'sources']);
  },
  addFeed: (feed) => request('/feeds', { method: 'POST', body: feed }),
  deleteFeed: (id) => request(`/feeds/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  refreshFeeds: (id) => request('/feeds/refresh', { method: 'POST', body: id == null ? {} : { id } }),
  async getFeedItems() {
    const data = await request('/feeds/items?limit=50');
    return listFrom(data, ['items', 'entries', 'articles']);
  },

  async getLinks() {
    const data = await request('/links');
    return listFrom(data, ['links', 'items', 'results']);
  },
  createLink: (link) => request('/links', { method: 'POST', body: link }),
  deleteLink: (code) => request(`/links/${encodeURIComponent(code)}`, { method: 'DELETE' }),

  async getReferenceIndex() {
    const data = await request('/reference');
    return listFrom(data, ['articles', 'items', 'references']);
  },
  getReference: (slug) => request(`/reference/${encodeURIComponent(slug)}`),
};
