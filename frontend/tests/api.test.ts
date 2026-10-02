import assert from 'node:assert/strict'
import test from 'node:test'
import { approveCase, createCase, initializeSession, refreshCase, resolveCase } from '../src/lib/api.ts'

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
      ? { environment: 'sandbox', approval_mode: 'test_operator', session_token: 'synthetic-session', csrf_token: 'synthetic-csrf', ai_runtime: { provider: 'groq', model: 'openai/gpt-oss-120b', location: 'hosted' } }
      : { case: { id: 'synthetic-case' } }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    const session = await initializeSession()
    assert.deepEqual(session.ai_runtime, { provider: 'groq', model: 'openai/gpt-oss-120b', location: 'hosted' })
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

test('intake preserves unknown facts and resolution sends only version-bound editable fields', async () => {
  const requests: { path: string; options: RequestInit }[] = []
  const original = globalThis.fetch
  globalThis.fetch = async (input, options = {}) => {
    requests.push({ path: String(input), options })
    return new Response(JSON.stringify({ case: { id: 'synthetic-case' } }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    await createCase({ capture_id: 'SYNTHETIC', customer_message: 'Unclear conversation', item_used: null, request_date: null })
    const resolution = { expected_version: 'revision-1', customer_message: 'Updated conversation', item_used: null, request_date: null, resolution_note: 'Awaiting merchant confirmation', amount_minor: 1 }
    await resolveCase('synthetic/case', resolution)
    assert.deepEqual(JSON.parse(String(requests[0].options.body)), { capture_id: 'SYNTHETIC', customer_message: 'Unclear conversation', item_used: null, request_date: null })
    assert.equal(requests[1].path, '/api/cases/synthetic%2Fcase/resolve')
    assert.deepEqual(JSON.parse(String(requests[1].options.body)), { expected_version: 'revision-1', customer_message: 'Updated conversation', item_used: null, request_date: null, resolution_note: 'Awaiting merchant confirmation' })
  } finally { globalThis.fetch = original }
})
