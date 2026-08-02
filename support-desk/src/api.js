/**
 * Client for the Python service in ../bot.
 *
 * Base URL comes from <meta name="api-base"> so it can change per deployment
 * without touching this file. When the panel is served by Laravel later, the
 * same endpoints move behind its own origin and the meta tag becomes empty.
 */

const META_BASE = document
  .querySelector('meta[name="api-base"]')
  ?.getAttribute('content')

export const API_BASE = (META_BASE ?? 'http://127.0.0.1:8000').replace(/\/$/, '')

/**
 * A bare name — no scheme, no dot — is not an address. The browser resolves it
 * relative to this page, so every call quietly hits the static site instead of
 * the API, which answers 200 with an empty body and produces an
 * incomprehensible JSON parse error.
 *
 * Render's `fromService … property: host` yields exactly that: a service name.
 */
const API_BASE_LOOKS_WRONG =
  !/^https?:\/\//.test(API_BASE) && !API_BASE.includes('.')

const MISCONFIGURED =
  `The panel is pointed at "${API_BASE}", which is not an address. ` +
  `Set VITE_API_BASE to the API's full URL (https://…) and rebuild with the ` +
  `build cache cleared.`

if (API_BASE_LOOKS_WRONG) {
  console.error(`[support-desk] ${MISCONFIGURED}`)
}

export class ApiError extends Error {
  constructor(message, { status = 0, offline = false, detail = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.offline = offline
    this.detail = detail
  }

  get unauthorized() {
    return this.status === 401
  }
}

/* ── Session ──────────────────────────────────────────────────────────────
   The token lives in localStorage rather than a cookie: the panel and the API
   are different origins in every environment, and a cross-site cookie brings
   SameSite problems this does not need. */

const TOKEN_KEY = 'support-desk.token'

export const session = {
  get token() {
    try {
      return localStorage.getItem(TOKEN_KEY)
    } catch {
      return null // private browsing with storage disabled
    }
  },
  set(token) {
    try {
      localStorage.setItem(TOKEN_KEY, token)
    } catch {
      /* nothing to do; the session simply will not persist */
    }
  },
  clear() {
    try {
      localStorage.removeItem(TOKEN_KEY)
    } catch {
      /* ignore */
    }
  },
  get signedIn() {
    return Boolean(this.token)
  },
}

/** Send the operator to the login page, remembering where they were headed. */
export function toLogin() {
  session.clear()
  const here = location.pathname + location.search
  const next = here.includes('login.html') ? '' : `?next=${encodeURIComponent(here)}`
  location.replace(`/login.html${next}`)
}

async function request(method, path, body, { anonymous = false } = {}) {
  if (API_BASE_LOOKS_WRONG) {
    throw new ApiError(MISCONFIGURED, { offline: true })
  }

  const headers = {}
  if (body) headers['Content-Type'] = 'application/json'
  if (!anonymous && session.token) {
    headers.Authorization = `Bearer ${session.token}`
  }

  let response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (cause) {
    // fetch only rejects on a network-level failure, which here almost always
    // means the service is not running. Say that, rather than "Failed to fetch".
    throw new ApiError('Cannot reach the support service.', { offline: true, cause })
  }

  if (!response.ok) {
    let detail = null
    try {
      detail = (await response.json())?.detail ?? null
    } catch {
      /* body was not JSON; the status alone is what we have */
    }

    const error = new ApiError(
      typeof detail === 'string' ? detail : `Request failed (${response.status}).`,
      { status: response.status, detail }
    )

    // An expired or missing session anywhere in the panel means one thing:
    // sign in again. Handled centrally so no caller has to remember.
    if (error.unauthorized && !anonymous) {
      toLogin()
    }
    throw error
  }

  if (response.status === 204) return null

  // Parse defensively. A 2xx with an empty or non-JSON body means we are not
  // talking to the API at all — usually a misconfigured base URL landing on a
  // static host, which answers 200 with nothing. Letting the raw parser error
  // through gives "Unexpected end of JSON input", which explains nothing.
  const text = await response.text()
  if (!text.trim()) {
    throw new ApiError(
      `The support service returned an empty reply from ${API_BASE}${path}. ` +
        `Check VITE_API_BASE points at the API, not the panel.`,
      { status: response.status }
    )
  }

  try {
    return JSON.parse(text)
  } catch {
    throw new ApiError(
      `The support service returned something that is not JSON from ` +
        `${API_BASE}${path}. Check VITE_API_BASE points at the API.`,
      { status: response.status }
    )
  }
}

export const api = {
  // Anonymous on purpose: these are how a session is obtained and checked.
  login: (username, password) =>
    request('POST', '/api/auth/login', { username, password }, { anonymous: true }),
  me: () => request('GET', '/api/auth/me'),

  health: () => request('GET', '/api/health', undefined, { anonymous: true }),
  dashboard: (period = 'week') => request('GET', `/api/dashboard?period=${period}`),
  queries: (channel, state) =>
    request(
      'GET',
      `/api/queries?channel=${encodeURIComponent(channel)}` +
        (state && state !== 'all' ? `&state=${state}` : '')
    ),
  reply: (ref, payload) => request('POST', `/api/queries/${encodeURIComponent(ref)}/reply`, payload),
  redraft: (ref) => request('POST', `/api/queries/${encodeURIComponent(ref)}/redraft`),
  newKeywords: () => request('GET', '/api/keywords/new'),
  settings: (channel) => request('GET', `/api/settings/${encodeURIComponent(channel)}`),
  saveSettings: (channel, payload) =>
    request('PUT', `/api/settings/${encodeURIComponent(channel)}`, payload),
  suggestKeywords: (channel, payload) =>
    request('POST', `/api/settings/${encodeURIComponent(channel)}/keywords/suggest`, payload),
}

/** Relative time, in the register the rest of the panel uses. */
export function when(iso) {
  if (!iso) return ''
  const then = new Date(iso)
  const seconds = Math.round((Date.now() - then.getTime()) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const days = Math.round(hours / 24)
  if (days === 1) return 'yesterday'
  if (days < 7) return `${days} days ago`
  return then.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

export const count = (n) => (n ?? 0).toLocaleString()

export function seconds(ms) {
  if (ms === null || ms === undefined) return '—'
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}
