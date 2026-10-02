import assert from 'node:assert/strict'
import test from 'node:test'
import { analysisRuntimeInfo, canApprove, conditionLabel, customerResponseDraft, merchantFacts, money, reviewRuntimeLabel, snapshotMatches, totals } from '../src/lib/refunds.ts'
import type { RefundCase } from '../src/types.ts'

function record(state = 'review_ready', provenance = 'paypal_sandbox', amount = 3900): RefundCase {
  return {
    id: 'synthetic-case', capture_id: 'SYNTHETICCAPTURE', state, created_at: '', case_version: 'revision-1', customer_message: 'Test fixture only',
    item_used: false, request_date: '2026-10-01', purchase_date: '', amount_minor: amount, currency: 'USD', transaction_provenance: provenance,
    transaction_verified_by_service: provenance === 'paypal_sandbox', physical_facts_provenance: 'merchant_supplied', capture_read_at: '',
    policy: { version: 'test', clauses: {} }, latest_review: { id: 'review', created_at: '', status: 'REVIEW_READY', review_hash: 'a'.repeat(64),
      accepted_recommendation: 'refund', rationale: '', citations: [], missing_information: [], errors: [], model_calls: 0,
      completed_model_responses: 0, runtime_provenance: 'injected_test_generator', error: null }, reviews: [],
    operation: state === 'review_ready' ? null : { id: 'op', state, refund_id: 'SYNTHETICREFUND', provider_status: 'COMPLETED', last_error: null,
      verification_provenance: provenance, approval: { approval_kind: 'test_operator', approved_by: 'test', approved_at: '',
        snapshot: { amount_minor: amount, currency: 'USD', review_hash: 'a'.repeat(64) } }, events: [] },
    events: [], can_review: false, can_approve: true, approval_kind: 'test_operator', approved_by: 'test', error: null,
  }
}

test('dashboard money excludes pending, uncertain, unsubmitted and injected evidence', () => {
  const actual = totals([record('verified'), record('pending'), record('uncertain'), record(), record('verified', 'injected_test_transport', 7000)])
  assert.equal(actual.total, 5)
  assert.equal(actual.verified, 1)
  assert.equal(actual.refundedMinor, 3900)
  assert.equal(totals([]).refundedMinor, 0)
})

test('money and approval reject unsafe integer values', () => {
  const unsafe = Number.MAX_SAFE_INTEGER + 1
  assert.equal(money(unsafe), 'Amount unavailable')
  assert.equal(canApprove(record('review_ready', 'paypal_sandbox', unsafe)), false)
  assert.ok(Number.isNaN(totals([record('verified', 'paypal_sandbox', unsafe)]).refundedMinor))
  assert.ok(Number.isNaN(totals([record('verified', 'paypal_sandbox', Number.MAX_SAFE_INTEGER), record('verified')]).refundedMinor))
})

test('approval snapshot binds capture, amount, currency and exact review', () => {
  const current = record()
  const snapshot = { caseId: current.id, caseVersion: current.case_version, captureId: current.capture_id, amountMinor: 3900, currency: 'USD', reviewHash: 'a'.repeat(64) }
  assert.equal(snapshotMatches(snapshot, current), true)
  for (const changed of [{ ...snapshot, amountMinor: 1 }, { ...snapshot, captureId: 'OTHER' }, { ...snapshot, currency: 'EUR' }, { ...snapshot, reviewHash: 'b'.repeat(64) }]) {
    assert.equal(snapshotMatches(changed, current), false)
  }
  assert.equal(snapshotMatches(snapshot, { ...current, can_approve: false }), false)
  assert.equal(snapshotMatches(snapshot, { ...current, case_version: 'revision-2' }), false)
})

test('unknown merchant facts stay unknown and cannot enable refund approval', () => {
  assert.deepEqual(merchantFacts('unknown', ''), { item_used: null, request_date: null })
  assert.deepEqual(merchantFacts('unused', '2026-10-01'), { item_used: false, request_date: '2026-10-01' })
  assert.deepEqual(merchantFacts('used', ''), { item_used: true, request_date: null })
  assert.equal(conditionLabel(null), 'Not confirmed')
  assert.equal(canApprove({ ...record(), item_used: null }), false)
  assert.equal(canApprove({ ...record(), request_date: null }), false)
  const current = record()
  assert.equal(canApprove({ ...current, latest_review: { ...current.latest_review!, status: 'REVIEW_NEEDS_INFORMATION', accepted_recommendation: 'request_information' } }), false)
})

test('no submit control is allowed after a payment has been claimed', () => {
  assert.equal(canApprove(record('prepared')), true)
  for (const state of ['submitting', 'pending', 'uncertain', 'verified', 'failed']) assert.equal(canApprove(record(state)), false)
})

test('hosted analysis discloses data transfer while historical labels use their saved runtime', () => {
  const hosted = analysisRuntimeInfo({ provider: 'groq', model: 'openai/gpt-oss-120b', location: 'hosted' })
  assert.equal(hosted.label, 'Hosted · Groq · openai/gpt-oss-120b')
  assert.equal(hosted.disclosure, 'Analyzing sends this conversation, payment summary, and saved policy to Groq.')
  const previous = { ...record().latest_review!, runtime_provenance: 'local_ollama', model: { model: 'saved-local-model' } }
  assert.equal(reviewRuntimeLabel(previous), 'Local · Ollama · saved-local-model')
  assert.equal(reviewRuntimeLabel({ ...previous, runtime_provenance: 'hosted_groq' }), 'Hosted · Groq · saved-local-model')
  assert.equal(reviewRuntimeLabel({ ...previous, runtime_provenance: null, model: null }), 'Runtime not recorded')
  assert.equal(analysisRuntimeInfo().label, 'AI provider unavailable')
})

test('customer drafts follow verified payment state and never repeat model status claims', () => {
  const current = record()
  current.latest_review!.evidence = { summary: '', claims: [], issues: [], reply_draft: 'Your refund is already complete.', validation_status: 'valid' }
  assert.equal(customerResponseDraft(current), null)
  current.latest_review!.evidence.reply_draft = 'Could you clarify which item was used?'
  current.latest_review!.evidence.reply_draft_source = 'application_template'
  assert.equal(customerResponseDraft(current), 'Could you clarify which item was used?')
  for (const state of ['submitting', 'pending', 'uncertain', 'failed']) {
    const changed = { ...current, operation: record(state).operation }
    assert.equal(customerResponseDraft(changed), 'The sandbox refund outcome has not been verified. A completed refund cannot be confirmed yet.')
  }
  assert.equal(customerResponseDraft(record('prepared')), 'A sandbox refund was approved, but no refund submission is recorded yet.')
  assert.equal(customerResponseDraft(record('verified', 'injected_test_transport')), 'This is a synthetic test result. No PayPal refund was made.')
  const completed = record('verified')
  completed.amount_minor = 9900
  assert.match(customerResponseDraft(completed)!, /completed refund of \$39\.00 USD/)
  assert.match(customerResponseDraft(completed)!, /SYNTHETICREFUND/)
  completed.operation!.provider_status = 'PENDING'
  assert.doesNotMatch(customerResponseDraft(completed)!, /confirmed a completed/)
})
