import type { NewRequest, Policy, RefundCase, Session } from '@/types'

let sessionToken: string | null = null
let csrfToken: string | null = null

export class ApiError extends Error {
  status: number
  constructor(message: string, status = 0) { super(message); this.status = status }
}

async function request<T>(path: string, body?: object, extraHeaders: Record<string, string> = {}): Promise<T> {
  const headers: Record<string, string> = { ...extraHeaders }
  if (sessionToken) headers['X-Refund-Desk-Session'] = sessionToken
  if (body) {
    headers['Content-Type'] = 'application/json'
    if (csrfToken) headers['X-Refund-Desk-CSRF'] = csrfToken
  }
  let response: Response
  try { response = await fetch(path, { method: body ? 'POST' : 'GET', headers, body: body ? JSON.stringify(body) : undefined, credentials: 'omit', cache: 'no-store' }) }
  catch { throw new ApiError('Connection interrupted. Reload the saved request before another action; a submitted refund may still be processing.') }
  let result: T & { error?: { message?: string } }
  try { result = await response.json() }
  catch { throw new ApiError('The response could not be read. Reload the saved request before another action.', response.status) }
  if (!response.ok) {
    if (response.status === 401) {
      sessionToken = csrfToken = null
      try { sessionStorage.removeItem('refund-desk-session') } catch { /* Memory-only sessions remain supported. */ }
    }
    throw new ApiError(result.error?.message || 'The request could not be completed.', response.status)
  }
  return result
}

export async function initializeSession(): Promise<Session> {
  const bootstrap = new URLSearchParams(location.hash.slice(1)).get('session')
  if (location.hash) history.replaceState(null, '', location.pathname)
  try { sessionToken = sessionStorage.getItem('refund-desk-session') } catch { /* Use the private launch link. */ }
  const session = await request<Session & { session_token: string; csrf_token: string }>(
    '/api/session', bootstrap ? {} : undefined, bootstrap ? { 'X-Refund-Desk-Bootstrap': bootstrap } : {})
  if (session.environment !== 'sandbox') throw new ApiError('This dashboard requires the PayPal sandbox environment.')
  sessionToken = session.session_token
  csrfToken = session.csrf_token
  try { sessionStorage.setItem('refund-desk-session', sessionToken) } catch { /* Keep the session in memory. */ }
  return { environment: session.environment, approval_mode: session.approval_mode }
}

export const listCases = () => request<{ cases: RefundCase[] }>('/api/cases')
export const getPolicy = () => request<{ policy: Policy }>('/api/policy')
export const getCase = (id: string) => request<{ case: RefundCase }>(`/api/cases/${encodeURIComponent(id)}`)
export const createCase = (body: NewRequest) => request<{ case: RefundCase }>('/api/cases', body)
export const reviewCase = (id: string) => request<{ case: RefundCase }>(`/api/cases/${encodeURIComponent(id)}/review`, {})
export const approveCase = (id: string, reviewHash: string) => request<{ case: RefundCase }>(`/api/cases/${encodeURIComponent(id)}/approve`, { review_hash: reviewHash, confirmed: true })
export const refreshCase = (id: string) => request<{ case: RefundCase }>(`/api/cases/${encodeURIComponent(id)}/refresh`, {})
