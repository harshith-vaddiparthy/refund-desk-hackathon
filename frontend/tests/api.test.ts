import assert from 'node:assert/strict'
import test from 'node:test'
import { approveCase, initializeSession, refreshCase } from '../src/lib/api.ts'

test('browser client sends only bound approval data and uses header auth without cookies', async () => {
  const requests: { path: string; options: RequestInit }[] = []
  let stored = ''
  Object.defineProperty(globalThis, 'location', { value: { hash: '#session=synthetic-bootstrap', pathname: '/' }, configurable: true })
  Object.defineProperty(globalThis, 'history', { value: { replaceState: () => {} }, configurable: true })
  Object.defineProperty(globalThis, 'sessionStorage', { value: { getItem: () => stored, setItem: (_: string, value: string) => { stored = value }, removeItem: () => { stored = '' } }, configurable: true })
  const original = globalThis.fetch
  globalThis.fetch = async (input, options = {}) => {
    requests.push({ path: String(input), options })
    return new Response(JSON.stringify(String(input) === '/api/session'
      ? { environment: 'sandbox', approval_mode: 'test_operator', session_token: 'synthetic-session', csrf_token: 'synthetic-csrf' }
      : { case: { id: 'synthetic-case' } }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    await initializeSession()
    await approveCase('synthetic-case', 'a'.repeat(64))
    await refreshCase('synthetic-case')
    assert.equal(requests.length, 3)
    assert.deepEqual(JSON.parse(String(requests[1].options.body)), { review_hash: 'a'.repeat(64), confirmed: true })
    assert.equal(requests[1].options.credentials, 'omit')
    assert.equal((requests[1].options.headers as Record<string, string>)['X-Refund-Desk-Session'], 'synthetic-session')
    assert.equal((requests[1].options.headers as Record<string, string>)['X-Refund-Desk-CSRF'], 'synthetic-csrf')
    assert.equal(requests[2].path, '/api/cases/synthetic-case/refresh')
    assert.deepEqual(JSON.parse(String(requests[2].options.body)), {})
  } finally { globalThis.fetch = original }
})

test('interrupted approval is not retried by the browser client', async () => {
  let calls = 0
  const original = globalThis.fetch
  globalThis.fetch = async () => { calls++; throw new TypeError('Synthetic disconnect') }
  try { await assert.rejects(approveCase('synthetic-case', 'a'.repeat(64)), /Connection interrupted/); assert.equal(calls, 1) }
  finally { globalThis.fetch = original }
})
