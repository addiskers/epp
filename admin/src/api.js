// Tiny fetch wrapper for /api/epp/*. Attaches the bearer token, JSON-encodes
// bodies, and throws Error(message) on non-2xx so callers can show it.

const TOKEN_KEY = 'epp_token'
const BASE = '/api/epp'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}
export function setToken(t) {
  if (t) localStorage.setItem(TOKEN_KEY, t)
  else localStorage.removeItem(TOKEN_KEY)
}

async function request(method, path, body, opts = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  let payload
  if (body !== undefined && body !== null) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  const res = await fetch(`${BASE}${path}`, { method, headers, body: payload })
  if (res.status === 401) {
    setToken('')
    throw new Error('Not authenticated')
  }
  const ct = res.headers.get('content-type') || ''
  if (opts.raw) return res
  const data = ct.includes('application/json') ? await res.json() : await res.text()
  if (!res.ok) {
    const detail = data && data.detail
    const msg = typeof detail === 'string' ? detail
      : (detail && detail.message) || (typeof data === 'string' ? data : 'Request failed')
    const err = new Error(msg)
    if (detail && typeof detail === 'object') err.detail = detail
    err.status = res.status
    throw err
  }
  return data
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b),
  patch: (p, b) => request('PATCH', p, b),
  del: (p, b) => request('DELETE', p, b),
  raw: (p) => request('GET', p, null, { raw: true }),
}

// Authenticated file download → browser save dialog.
export async function downloadFile(path, filename) {
  const res = await fetch(`${BASE}${path}`, { headers: { Authorization: `Bearer ${getToken()}` } })
  if (!res.ok) throw new Error('Download failed')
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}

// URL an <audio> element can stream from (it cannot send an Authorization header).
export function audioUrl(path) {
  return `${BASE}${path}?token=${encodeURIComponent(getToken())}`
}

// Build a query string from a filters object (skips empty values).
export function qs(params) {
  const u = new URLSearchParams()
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== '') u.set(k, v)
  }
  const s = u.toString()
  return s ? `?${s}` : ''
}

// Shared formatting helpers.
export function fmtDate(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}
export function fmtDur(s) {
  s = Math.round(Number(s) || 0)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}:${String(s % 60).padStart(2, '0')}`
}
export const STATUS_LABEL = {
  open: 'Open', under_review: 'Under Review', escalated: 'Escalated', resolved: 'Resolved', closed: 'Closed',
}
export const STATUSES = Object.keys(STATUS_LABEL)
export const CALLER_TYPES = ['customer', 'vendor', 'employee']
export const LANG_NAME = {
  en: 'English', hi: 'Hindi', gu: 'Gujarati', mr: 'Marathi', bn: 'Bengali', ta: 'Tamil', te: 'Telugu',
  kn: 'Kannada', ml: 'Malayalam', pa: 'Punjabi', or: 'Odia', as: 'Assamese',
}
export function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : '' }
